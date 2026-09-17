# -*- coding: utf-8 -*-
"""scan.py —— 每日确定性管道:信号复算→硬过滤→桶排序→池内跟踪→日报(spec §4)。
唯一数据源=indicators parquet(路线B);layers.signal 原封复用,尾部策略滚动现场算。
筛选核心(assemble/iter_whitelist/env_label/collect_candidates/screen)+ 画像卡/主流程/CLI(Task 6)。"""
import glob
import json
import os
import statistics
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)          # CLI 直跑 python pool/scan.py 时需可导入 pool 包

from pool import cfg as C             # noqa: E402(需先修 sys.path)
from pool import pool as P            # noqa: E402
from pool.lookup import bucket_key, build_lookup, feature_series, load_lookup  # noqa: E402

RAW_COLS = ["open", "high", "low", "close", "volume"]
MIN_AMOUNT = 2e8          # 流动性:当日成交额(真实,元)
MAX_CHG = 0.097           # 收盘涨停代理(≥9.7% 剔除,次日大概率买不进)
TAIL = 500                # 回看窗口(MA240+EMA收敛余量)


def assemble(df):
    """parquet → layers 输入:原始5列 + j=kdj_j(同批量口径,零重算)。"""
    g = df[RAW_COLS].copy()
    g["j"] = df["kdj_j"]
    return g


def iter_whitelist(parq_dir=None):
    parq_dir = parq_dir or C.PARQ_DIR
    for fp in sorted(glob.glob(os.path.join(parq_dir, "*.parquet"))):
        sym = os.path.basename(fp)[:-8]
        if sym.startswith(("sh60", "sz00")):
            yield fp, sym


def env_label(parq_dir=None):
    """sh000300 vs MA240 → (bull, 指数末日, 指数日涨跌)。指数数据可能滞后一天(已知)。"""
    parq_dir = parq_dir or C.PARQ_DIR
    sh = pd.read_parquet(os.path.join(parq_dir, "sh000300.parquet"))
    bull = bool(sh["close"].iloc[-1] > sh["ma240"].iloc[-1])
    chg = float(sh["close"].iloc[-1] / sh["close"].iloc[-2] - 1)
    return bull, sh.index[-1], chg


def collect_candidates(parq_dir=None, expected_date=None, signal_fn=None, tail=TAIL):
    """复算当日信号并提取特征。signal_fn 可注入(测试);expected_date 非 None 时
    末日不符的票记为异常(停牌/滞后)。返回 (cands, anomalies)。"""
    import kdj.layers as L
    from kdj.layers import DEFAULT_CFG
    parq_dir = parq_dir or C.PARQ_DIR
    sig_fn = signal_fn or (lambda g, cfg: L.signal(g, cfg))
    cands, anomalies = [], []
    for fp, sym in iter_whitelist(parq_dir):
        try:
            df = pd.read_parquet(fp)
            if expected_date and df.index[-1] != expected_date:
                anomalies.append((sym, "末日 %s ≠ %s(停牌/滞后)" % (df.index[-1], expected_date)))
                continue
            g = assemble(df.tail(tail))
            sig = sig_fn(g, DEFAULT_CFG)
            if not bool(sig.iloc[-1]):
                continue
            prev = g["close"].iloc[-2]
            cur = g["close"].iloc[-1]
            fs = feature_series(g)
            cands.append({"sym": sym, "date": g.index[-1], "close": float(cur),
                          "prev_close": float(prev), "chg": float(cur / prev - 1),
                          "amount": float(df["amount"].iloc[-1] / df["factor"].iloc[-1] * 1000),
                          "feats": {k: (float(s.iloc[-1]) if s.iloc[-1] == s.iloc[-1] else None)
                                    for k, s in fs.items()}})
        except Exception as e:                                  # 单票异常不炸整体
            anomalies.append((sym, repr(e)))
    return cands, anomalies


def screen(cands, lookup_data, top=30, min_amount=MIN_AMOUNT, max_chg=MAX_CHG):
    """硬过滤(成交额/涨停代理)+ 同型桶胜率排序(spec §6)。返回 (top_list, 剔除数)。
    n≥30 桶用其 win_rate;<30/缺桶置全部达标桶 win_rate 的中位数(不加分)。
    排序:score 降序 → 有达标桶统计者在前 → 并列按成交额降序(中位分不应挤掉真实桶同分票)。"""
    alive = [c for c in cands if c["amount"] >= min_amount and c["chg"] < max_chg]
    n_rej = len(cands) - len(alive)
    tbl = lookup_data.get("buckets", {})
    scores = [b["win_rate"] for b in tbl.values() if b.get("n", 0) >= 30]
    med = statistics.median(scores) if scores else 0.5         # <30 样本桶置中位不加分

    def qualified(c):
        st = c.get("bucket_stat")
        return st is not None and st.get("n", 0) >= 30

    for c in alive:
        c["bucket_stat"] = tbl.get(c["bucket"])
        n = c["bucket_stat"]["n"] if c["bucket_stat"] else 0
        c["score"] = c["bucket_stat"]["win_rate"] if c["bucket_stat"] and n >= 30 else med
    alive.sort(key=lambda c: (-c["score"], 0 if qualified(c) else 1, -c["amount"]))
    return alive[:top], n_rej


# (接筛选核心) —— 画像卡/主流程/CLI
PARQ_TAIL = TAIL          # 测试可覆写
SENTINELS = ("sh600000", "sz000651")     # 滞后哨兵:几乎不停牌的活跃股


def _read_calendar():
    with open(C.CALENDAR, encoding="utf-8") as f:
        return [x for x in f.read().split() if x]


def profile_card(cand, name=""):
    b = cand.get("bucket_stat") or {}
    wr = "%.1f%%" % (100 * b["win_rate"]) if b.get("win_rate") is not None else "—"
    po = "%.2f" % b["payoff"] if b.get("payoff") is not None else "—"
    f = cand["feats"]
    return ("- **%s %s** 收盘 %.2f(%+.1f%%)/现价 %.2f | 环境 %s | J低 %.1f | 回撤 %.1f%% | "
            "趋势年龄 %s | 缩量比 %s | 桶 %s(胜率 %s 盈亏比 %s 样本 %s) | 成交额 %.1f亿"
            % (cand["sym"], name, cand["close"], 100 * cand["chg"], cand.get("raw_close", 0.0),
               cand.get("env", "—"), f.get("j_low") if f.get("j_low") is not None else float("nan"),
               100 * f["dd"] if f.get("dd") is not None else float("nan"),
               f.get("age"), f.get("shrink"), cand.get("bucket", "—"), wr, po, b.get("n", 0),
               cand["amount"] / 1e8))


def _tracking_bars(df, entry, today):
    """该票 > last_date 且 > added 的K线(后复权)+ T+1 起至今交易日数(按市场日历)。"""
    cal = _read_calendar()
    lo = max(entry["added"], entry["last_date"] or entry["added"])
    bars = [(d, float(df.at[d, "high"]), float(df.at[d, "low"]), float(df.at[d, "close"]))
            for d in df.index if d > lo and d <= today]
    i_add = cal.index(entry["added"]) if entry["added"] in cal else None
    i_today = cal.index(today) if today in cal else None
    n_elapsed = (i_today - i_add) if (i_add is not None and i_today is not None) else len(bars)
    return bars, max(n_elapsed, 0)


def main_scan(parq_dir=None, pool_path=None, report_dir=None, now_date=None):
    parq_dir = parq_dir or C.PARQ_DIR
    pool_path = pool_path or C.POOL_PATH
    report_dir = report_dir or C.REPORT_DIR
    ver = C.cfg_version()
    cal = _read_calendar()
    expected = now_date or cal[-1]
    os.makedirs(report_dir, exist_ok=True)

    # 数据滞后检查(哨兵股)
    stale = [s for s in SENTINELS
             if (lambda p: not os.path.isfile(p)
                 or pd.read_parquet(p).index[-1] < expected)
             (os.path.join(parq_dir, "%s.parquet" % s))]
    if stale:
        with open(os.path.join(report_dir, "%s.md" % expected), "w", encoding="utf-8") as f:
            f.write("# 观察池日报 %s\n\n⚠ 数据滞后:哨兵 %s 未到 %s,请先跑 daily_update.sh\n"
                    % (expected, ",".join(stale), expected))
        print("数据滞后(%s)" % stale)
        return 1

    bull, sh_date, sh_chg = env_label(parq_dir)
    lk = load_lookup(ver)
    if lk is None:
        print("lookup 表缺失,首次生成(慢)…")
        lk = build_lookup(parq_dir=parq_dir, cfg_ver=ver)

    cands, anomalies = collect_candidates(parq_dir, expected_date=expected)
    for c in cands:
        c["env"] = "牛性" if bull else "熊性"
        c["bucket"] = "|".join(bucket_key(bull, c["feats"]["j_low"], c["feats"]["dd"]))
        fp = os.path.join(parq_dir, "%s.parquet" % c["sym"])
        pdf = pd.read_parquet(fp, columns=["factor"])
        c["raw_close"] = float(c["close"] / pdf["factor"].iloc[-1])
    top, n_rej = screen(cands, lk)

    # 池内跟踪与结案
    state = P.load_pool(pool_path)
    closed_today = []
    for e in P.watching(state):
        fp = os.path.join(parq_dir, _sym_path(e["code"]))
        try:
            df = pd.read_parquet(fp)
        except FileNotFoundError:
            anomalies.append((e["code"], "parquet 缺失"))
            continue
        bars, n_el = _tracking_bars(df, e, expected)
        o = P.track(e, bars, n_el)
        if o:
            closed_today.append((e, o))
    P.save_pool(state, pool_path)

    # candidates JSON(入池校验的机器可读源)
    cj = {"date": expected, "cfg_version": ver,
          "candidates": [{k: c.get(k) for k in
                          ("sym", "close", "raw_close", "chg", "amount", "bucket", "score",
                           "bucket_stat", "feats", "env")} for c in top]}
    with open(os.path.join(report_dir, "candidates-%s.json" % expected.replace("-", "")),
              "w", encoding="utf-8") as f:
        json.dump(cj, f, ensure_ascii=False, indent=1)

    # 日报(spec §8)
    lines = ["# 观察池日报 %s" % expected, "",
             "## 1. 环境",
             "沪深300 vs MA240:**%s**(%s 收盘,日涨跌 %+.2f%%;指数数据日 %s)"
             % ("牛性" if bull else "熊性", sh_date, 100 * sh_chg, sh_date), "",
             "## 2. 今日候选",
             "信号 %d 只;硬过滤剔除 %d;取前 %d(不足则如实列数:本日 %d 只)"
             % (len(cands), n_rej, len(top), len(top)), ""]
    lines += [profile_card(c) for c in top] or ["- 无"]
    lines += ["", "## 3. 池内动态"]
    w = P.watching(state)
    lines += ["- %s 入池 %s 基准 %.2f | 极值 %+.1f%%/%+.1f%% | 已过 %d 日"
              % (e["code"], e["added"], e["add_close"], 100 * e["max_up"], 100 * e["min_dn"],
                 e.get("days", 0)) for e in w] or ["- 池空"]
    lines += ["", "## 4. 今日结案"]
    lines += ["- %s → **%s**(历时 %d 日,极值 %+.1f%%/%+.1f%%)"
              % (e["code"], o["type"], o["days"], 100 * o["max_up"], 100 * o["min_dn"])
              for e, o in closed_today] or ["- 无"]
    lines += ["", "## 5. 异常票 / 数据说明"]
    lines += ["- %s: %s" % a for a in anomalies] or ["- 无"]
    lines += ["", "## 6. AI 研究区", "", ">(\"研究今天的候选\"后由 AI 追加研究卡与推荐理由)", ""]
    with open(os.path.join(report_dir, "%s.md" % expected), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("scan 完成:候选 %d(剔 %d) 结案 %d → %s"
          % (len(top), n_rej, len(closed_today), os.path.join(report_dir, expected + ".md")))
    return 0


def _sym_path(code):
    pre = "sh" if code.startswith("6") else "sz"
    return "%s%s.parquet" % (pre, code)


def snapshot(code, date=None):
    """单票取数(skill 专用,JSON):现价/涨跌/技术快照/环境。"""
    fp = os.path.join(C.PARQ_DIR, _sym_path(code))
    df = pd.read_parquet(fp)
    i = list(df.index).index(date) if date else -1
    g = assemble(df)
    from pool.lookup import signal_features
    feats = signal_features(g, i)
    bull, _, _ = env_label()
    prev = df["close"].iloc[i - 1]
    return {"code": code, "date": df.index[i],
            "close": float(df["close"].iloc[i]),
            "raw_close": float(df["close"].iloc[i] / df["factor"].iloc[i]),
            "chg": float(df["close"].iloc[i] / prev - 1),
            "amount": float(df["amount"].iloc[i] / df["factor"].iloc[i] * 1000),
            "env": "牛性" if bull else "熊性", "feats": feats,
            "ma240": float(df["ma240"].iloc[i]) if "ma240" in df else None,
            "boll_mid": float(df["boll_mid"].iloc[i]) if "boll_mid" in df else None,
            "atr14": float(df["atr14"].iloc[i]) if "atr14" in df else None,
            "macd_hist": float(df["macd_hist"].iloc[i]) if "macd_hist" in df else None}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("snapshot")
    sp.add_argument("code")
    sp.add_argument("--date", default=None)
    a = ap.parse_args()
    if a.cmd == "snapshot":
        print(json.dumps(snapshot(a.code, a.date), ensure_ascii=False))
    else:
        sys.exit(main_scan())
