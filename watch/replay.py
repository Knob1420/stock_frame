# -*- coding: utf-8 -*-
"""watchlist 提醒链路历史回放：规则序列 → 状态机 → 评分门槛冷却 → 前三名。

与 scan.py 共享规则注册表和评分函数；规则重实现为整列布尔序列以支持按日回放。
weekly_b1 用增量递推复刻"当日含运行中周 bar"的精确口径（KDJ/EMA 均为递归式，
运行 bar 可从上一完整周一步外推）。

用法:
  python watch/replay.py --verify            # 规则序列与 scan.py 逐条规则断言一致
  python watch/replay.py                     # 2020-01-01 起全链路回放 → temp/replay_events.parquet
  python watch/replay.py --start 2022-01-01  # 自定义起点
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(HERE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import scan  # noqa: E402  复用 _score/_rule_descriptor/_tier/常量
from kdj.layers import baseline_original, ema2, long_ma  # noqa: E402
from rules import rule_b1_hook, rule_weekly_b1  # noqa: E402

IND = ROOT / "stockdata" / "indicators"
CONFIG_F = HERE / "watchlist.yaml"
OUT_F = ROOT / "temp" / "replay_events.parquet"
COST = 0.0015  # 双边成本假设(印花税+佣金+冲击)


# ---------------------------------------------------------------- 规则序列版

def s_b1(df, p):
    d = df.assign(j=df["kdj_j"])
    if p.get("no_good"):
        return d["j"].le(15).fillna(False)
    return baseline_original(d).fillna(False)


def s_b1_hook(df, p):
    d = df.assign(j=df["kdj_j"])
    j = d["j"].to_numpy()
    good = (ema2(d["close"]) > long_ma(d["close"])).fillna(False).to_numpy()
    pct = (d["close"] / d["close"].shift(1) - 1).abs().le(
        float(p.get("max_abs_pct", 0.03))).fillna(False).to_numpy()
    amp = ((d["high"] - d["low"]) / d["close"].shift(1)).le(
        float(p.get("max_amplitude", 0.07))).fillna(False).to_numpy()
    up = np.r_[False, j[1:] > j[:-1]]
    if not p.get("require_good", True):
        good = np.ones(len(j), bool)
    out = np.zeros(len(j), bool)
    last_low, ups_since = -1, 0
    for t in range(len(j)):
        if j[t] <= float(p.get("threshold", 15.0)):
            last_low, ups_since = t, 0
        if up[t]:
            ups_since += 1
        out[t] = up[t] and last_low >= 0 and ups_since == 1 and good[t] and pct[t] and amp[t]
    return pd.Series(out, index=df.index)


def s_weekly_b1(df, p):
    """按日输出"截至当日(含运行中周bar)的周线B1"，增量递推，与逐日全量重算等价。"""
    no_good = bool(p.get("no_good", False))
    n = len(df)
    out = np.zeros(n, bool)
    periods = df.index.to_period("W-FRI")
    high, low, close = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    alpha = 2.0 / 11.0                                   # ewm(span=10, adjust=False)
    wh, wl, wc = [], [], []                              # 已完整周
    K = D = e1 = e2 = None                               # 上一完整周末端值
    cur = None                                           # 运行中周聚合
    for t in range(n):
        if cur is None or periods[t] != cur["p"]:
            if cur is not None:                          # 上周收线，固化为完整周
                wh.append(cur["h"]); wl.append(cur["l"]); wc.append(cur["c"])
            cur = {"p": periods[t], "h": high[t], "l": low[t], "c": close[t]}
        else:
            cur["h"] = max(cur["h"], high[t])
            cur["l"] = min(cur["l"], low[t])
            cur["c"] = close[t]
        if not wc:                                       # pct 无前周收盘 → 信号必为 False
            continue
        c_prev = wc[-1]
        pct = cur["c"] / c_prev - 1.0
        amp = (cur["h"] - cur["l"]) / c_prev
        llv = min(wl[-8:] + [cur["l"]])
        hhv = max(wh[-8:] + [cur["h"]])
        if hhv > llv:
            rsv = (cur["c"] - llv) / (hhv - llv) * 100.0
            K_run = rsv if K is None else K + (rsv - K) / 3.0
            D_run = K_run if D is None else D + (K_run - D) / 3.0
        else:                                            # ewm 跳过 NaN → 沿用前值
            K_run, D_run = K, D
        j_run = 3 * K_run - 2 * D_run if K_run is not None else np.nan
        e1_run = cur["c"] if e1 is None else e1 + alpha * (cur["c"] - e1)
        e2_run = e1_run if e2 is None else e2 + alpha * (e1_run - e2)
        mas = []
        for win in (14, 28, 57, 114):
            tail = wc[-(win - 1):] + [cur["c"]] if win > 1 else [cur["c"]]
            mas.append(sum(tail) / len(tail) if len(tail) == win else np.nan)
        good = e2_run > sum(mas) / 4.0                   # 任一 MA 不足 → NaN → False
        ok_j = (j_run <= 15) if j_run == j_run else False
        out[t] = ok_j if no_good else (
            bool(good) and ok_j and abs(pct) <= 0.03 and amp <= 0.07
        )
        # 周内最后一日过后值即定；下周首日开户时再固化
        if t + 1 < n and periods[t + 1] != cur["p"]:
            K, D, e1, e2 = K_run, D_run, e1_run, e2_run
    return pd.Series(out, index=df.index)


def s_near_ma(df, p):
    n, tol = int(p.get("period", 60)), float(p.get("tolerance", 0.02))
    ma = df["close"].rolling(n, min_periods=n).mean()
    r = df["close"] / ma - 1.0
    near = r.abs().le(tol) & (df["close"] >= ma)
    if p.get("direction") == "down":
        near = r.abs().le(tol) & (df["close"] < ma)
    return near.fillna(False)


def s_volume_dry_up(df, p):
    if "ratio" in p:
        vma = df["volume"].rolling(20, min_periods=20).mean()
        return (df["volume"] / vma <= float(p["ratio"])).fillna(False)
    return df["volume"] < df["volume"].rolling(250, min_periods=250).quantile(
        float(p.get("pct", 0.2)))                                        # ponytail: 配置未用到


def s_box_touch_lower(df, p):
    lower = float(p["lower"])
    f = df["factor"].to_numpy()
    return (df["low"].to_numpy() / f <= lower * 1.01) & (
        df["close"].to_numpy() / f >= lower * 0.97)


def s_box_breakdown(df, p):
    lower = float(p["lower"])
    now = df["close"].to_numpy() / df["factor"].to_numpy()
    prev = np.r_[now[0], now[:-1]]
    out = (prev >= lower) & (now < lower)
    out[0] = False
    return pd.Series(out, index=df.index)


SERIES = {
    "b1": s_b1,
    "b1_hook": s_b1_hook,
    "weekly_b1": s_weekly_b1,
    "near_ma": s_near_ma,
    "volume_dry_up": s_volume_dry_up,
    "box_touch_lower": s_box_touch_lower,
    "box_breakdown": s_box_breakdown,
}


# ---------------------------------------------------------------- 回放引擎

def _abs_or_zero(x):
    return abs(x) if x == x else 0.0


def load_config(config_path=CONFIG_F):
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    defaults = config.get("defaults", {}) or {}
    alerting = {**scan.DEFAULT_ALERTING, **(defaults.get("alerting", {}) or {})}
    stocks = []
    for item in config.get("watchlist", []):
        if item.get("enabled", True) is False:
            continue
        rules = ([] if item.get("inherit_defaults", True) is False
                 else list(defaults.get("rules", []))) + list(item.get("rules", []))
        desc = [scan._rule_descriptor(spec) for spec in rules]
        stocks.append({
            "code": item["code"],
            "name": item.get("name", ""),
            "priority": item.get("priority", "normal"),
            "descs": desc,
        })
    return stocks, alerting


def prepare(stock, indicator_dir=IND):
    """读数据并预计算：每条规则布尔序列、dist_ma20、价格数组、前向收益。"""
    df = pd.read_parquet(Path(indicator_dir) / (stock["code"] + ".parquet"))
    df.index = pd.DatetimeIndex(df.index)
    c, h, l = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    ma20 = df["close"].rolling(20, min_periods=20).mean().to_numpy()
    prep = {
        "index": df.index,
        "pos": {ts.normalize(): i for i, ts in enumerate(df.index)},
        "series": {
            "%s|%s|%s" % (stock["code"], d["rule"], d["hint"]):
                np.asarray(SERIES[d["rule"]](df, d["params"]), dtype=bool)
            for d in stock["descs"] if d["rule"] in SERIES
        },
        "dist_ma20": c / ma20 - 1.0,
        "close": c,
        "fwd": {                                       # 位置 → (ret5, ret10, ret20, mfe20, mae20)
            i: (
                c[i + 5] / c[i] - 1 if i + 5 < len(c) else np.nan,
                c[i + 10] / c[i] - 1 if i + 10 < len(c) else np.nan,
                c[i + 20] / c[i] - 1 if i + 20 < len(c) else np.nan,
                h[i + 1:i + 21].max() / c[i] - 1 if i + 20 < len(c) else np.nan,
                l[i + 1:i + 21].min() / c[i] - 1 if i + 20 < len(c) else np.nan,
            )
            for i in range(len(c))
        },
    }
    return prep


def replay(start="2020-01-01", end=None, config_path=CONFIG_F, indicator_dir=IND):
    stocks, alerting = load_config(config_path)
    start = pd.Timestamp(start)
    preps = {}
    for s in stocks:
        path = Path(indicator_dir) / (s["code"] + ".parquet")
        if not path.exists():
            print("⚠ %s 无数据，跳过" % s["code"], file=sys.stderr)
            continue
        preps[s["code"]] = prepare(s, indicator_dir)

    # 热身：用 start 前历史建立规则激活状态（等价生产连续运行）
    active_prev, last_alert = {}, {}
    for s in stocks:
        code = s["code"]
        if code not in preps:
            continue
        p = preps[code]
        warm = int((p["index"] < start).sum())
        if warm == 0:
            continue
        if warm == len(p["index"]):                    # 全部历史都在 start 前
            continue
        for key, arr in p["series"].items():
            active_prev[key] = bool(arr[warm - 1])

    end = pd.Timestamp(end) if end else max(p["index"].max() for p in preps.values())
    calendar = sorted({ts for p in preps.values() for ts in p["index"] if start <= ts <= end})
    rows = []
    for ts in calendar:
        day = ts.normalize()
        candidates = []
        for s in stocks:
            code = s["code"]
            p = preps.get(code)
            if p is None or day not in p["pos"]:
                continue
            i = p["pos"][day]
            new_rules, active_rules = [], []
            for d in s["descs"]:
                key = "%s|%s|%s" % (code, d["rule"], d["hint"])
                if key not in p["series"]:
                    continue
                on = bool(p["series"][key][i])
                if on:
                    active_rules.append(d)
                    if not active_prev.get(key, False):
                        new_rules.append(d)
                active_prev[key] = on
            if not new_rules:
                continue
            score, categories = scan._score(active_rules, s["priority"])
            standalone = any(x["standalone"] for x in new_rules)
            if not standalone and (score < int(alerting["min_score"])
                                   or len(categories) < int(alerting["min_categories"])):
                rows.append(_row(s, ts, score, "组合观察" if score >= 6 else "弱变化",
                                 "threshold_suppressed", new_rules, p, i))
                continue
            kind = "risk" if any(x["kind"] == "risk" for x in new_rules) else "watch"
            tier = scan._tier({"kind": kind, "score": score, "new_rules": new_rules})
            la = last_alert.get(code)
            bars_since = (i - la["pos"]) if la else 10 ** 9
            jump = score - (la["score"] if la else -99)
            if (bars_since < int(alerting["cooldown_bars"]) and kind != "risk"
                    and jump < int(alerting["cooldown_override_score"])):
                rows.append(_row(s, ts, score, tier, "cooldown_suppressed",
                                 new_rules, p, i))
                continue
            candidates.append((s, p, i, score, tier, kind, new_rules))
        candidates.sort(key=lambda x: (
            x[5] == "risk",
            scan.PRIORITY_ORDER.get(x[0]["priority"], 1),
            x[3], -_abs_or_zero(x[1]["dist_ma20"][x[2]]), x[0]["code"]), reverse=True)
        max_push = int(alerting["max_push_stocks"])
        for rank, (s, p, i, score, tier, kind, new_rules) in enumerate(candidates):
            status = "selected" if rank < max_push else "cap_suppressed"
            if status == "selected":
                last_alert[s["code"]] = {"pos": i, "score": score}
            rows.append(_row(s, ts, score, tier, status, new_rules, p, i, rank))

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame = _add_universe_excess(frame, preps, calendar)
    return frame


def _row(s, ts, score, tier, status, new_rules, p, i, rank=None):
    ret5, ret10, ret20, mfe, mae = p["fwd"][i]
    return {
        "date": str(ts.date()), "code": s["code"], "name": s["name"],
        "priority": s["priority"], "score": score, "tier": tier,
        "kind": "risk" if status != "threshold_suppressed" and tier == "风险提醒" else "watch",
        "delivery_status": status, "rank": rank,
        "new_rule": "+".join(x["label"] for x in new_rules),
        "close": p["close"][i], "dist_ma20": p["dist_ma20"][i],
        "ret5": ret5, "ret10": ret10, "ret20": ret20, "mfe20": mfe, "mae20": mae,
    }


def _add_universe_excess(frame, preps, calendar):
    """按日全 watchlist 均值作基准，事件超额 = 事件收益 − 当日基准。"""
    base = {}
    for k, col, slot in ((5, "ret5", 0), (10, "ret10", 1), (20, "ret20", 2)):
        vals = {}
        for ts in calendar:
            day = ts.normalize()
            rets = [p["fwd"][p["pos"][day]][slot]
                    for p in preps.values() if day in p["pos"]]
            rets = [x for x in rets if x == x]
            if rets:
                vals[str(ts.date())] = sum(rets) / len(rets)
        base[col] = vals
    for col in ("ret5", "ret10", "ret20"):
        frame["excess_" + col] = frame.apply(
            lambda r: r[col] - base[col].get(r["date"], np.nan), axis=1)
    return frame


# ---------------------------------------------------------------- 校验

def verify(config_path=CONFIG_F, indicator_dir=IND):
    """断言：序列版末日值 == scan.py 逐条规则函数；周线增量 == 全量重算(抽样)。"""
    from rules import RULES
    stocks, alerting = load_config(config_path)
    rng = np.random.default_rng(7)
    checked = probed = 0
    for si, s in enumerate(stocks):
        path = Path(indicator_dir) / (s["code"] + ".parquet")
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        df.index = pd.DatetimeIndex(df.index)
        for d in s["descs"]:
            if d["rule"] not in SERIES:
                continue
            series = SERIES[d["rule"]](df, d["params"])
            last = series[-1] if isinstance(series, np.ndarray) else series.iloc[-1]
            assert bool(last) == bool(RULES[d["rule"]](df, d["params"])), \
                "%s %s 末日不一致" % (s["code"], d["rule"])
            checked += 1
        if si % 12:            # 全量重算昂贵，周线抽样只取约 1/12 的股票
            continue
        wser = np.asarray(s_weekly_b1(df, {}))
        probe = [i for i in range(1, len(df))
                 if df.index[i].weekday() < df.index[i - 1].weekday()]
        probe += list(rng.choice(len(df), size=min(10, len(df)), replace=False))
        for i in sorted(set(probe)):
            full = rule_weekly_b1(df.iloc[: i + 1], {})
            assert bool(wser[i]) == bool(full), \
                "%s weekly_b1 第%d日不一致" % (s["code"], i)
            probed += 1
    print("✓ %d 条规则末日值与 scan.py 一致；weekly_b1 抽样 %d 天全量重算一致"
          % (checked, probed))

    # 端到端：末日 preview 语义对比 scan.run(preview=True)
    mine = _preview_last_day(stocks, alerting, indicator_dir)
    ref = scan.run(preview=True, persist=False, config_path=config_path,
                   indicator_dir=indicator_dir)
    ref_q = sorted((e["code"], e["score"]) for e in ref["qualified"])
    ref_s = [e["code"] for e in ref["events"]]
    assert mine["qualified"] == ref_q, "qualified 不一致:\n%s\n%s" % (mine["qualified"], ref_q)
    assert mine["selected"] == ref_s, "selected 不一致: %s vs %s" % (mine["selected"], ref_s)
    print("✓ 末日 preview 对比 scan.run：qualified %d 只、selected %s 一致"
          % (len(ref_q), ref_s))


def _preview_last_day(stocks, alerting, indicator_dir):
    """把末日所有激活条件当新变化(无冷却)重算一遍，对比 scan 的 preview。"""
    qualified = []
    for s in stocks:
        path = Path(indicator_dir) / (s["code"] + ".parquet")
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        df.index = pd.DatetimeIndex(df.index)
        active = []
        for d in s["descs"]:
            if d["rule"] not in SERIES:
                continue
            ser = SERIES[d["rule"]](df, d["params"])
            last = ser[-1] if isinstance(ser, np.ndarray) else ser.iloc[-1]
            if bool(last):
                active.append(d)
        if not active:
            continue
        score, cats = scan._score(active, s["priority"])
        standalone = any(x["standalone"] for x in active)
        if not standalone and (score < int(alerting["min_score"])
                               or len(cats) < int(alerting["min_categories"])):
            continue
        ma20 = df["close"].rolling(20, min_periods=20).mean().iloc[-1]
        qualified.append((s, score, bool(any(x["kind"] == "risk" for x in active)),
                          -_abs_or_zero(df["close"].iloc[-1] / ma20 - 1)))
    qualified.sort(key=lambda x: (
        x[2], scan.PRIORITY_ORDER.get(x[0]["priority"], 1), x[1], x[3],
        x[0]["code"]), reverse=True)
    pairs = [(x[0]["code"], x[1]) for x in qualified]
    return {"qualified": sorted(pairs), "selected": [c for c, _ in pairs[:3]]}


# ---------------------------------------------------------------- 主入口

def summarize(frame):
    if frame.empty:
        print("无事件")
        return
    print("事件总数 %d（%s）" % (len(frame), frame["date"].min() + " ~ " + frame["date"].max()))
    for status, g in frame.groupby("delivery_status"):
        ex = g["excess_ret10"]
        ex = ex[ex.notna()]
        print("%-20s n=%-4d excess10均值%+.2f%% 中位%+.2f%% t=%.2f | mae20均值%+.2f%%" % (
            status, len(g), 100 * ex.mean(), 100 * ex.median(),
            ex.mean() / (ex.std() / np.sqrt(len(ex))) if len(ex) > 2 else np.nan,
            100 * g["mae20"].mean()))
    for lo, hi in ((4, 5), (6, 7), (8, 9), (10, 99)):
        g = frame[(frame["score"] >= lo) & (frame["score"] <= hi)
                  & (frame["delivery_status"].isin(("selected", "cap_suppressed")))]
        ex = g["excess_ret10"].dropna()
        if len(ex) >= 3:
            print("分数%d-%2d  n=%-4d excess10 %+.2f%% (t=%.2f)" % (
                lo, hi, len(ex), 100 * ex.mean(),
                ex.mean() / (ex.std() / np.sqrt(len(ex)))))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    args = parser.parse_args(argv)
    if args.verify:
        verify()
        return 0
    frame = replay(args.start, args.end)
    OUT_F.parent.mkdir(exist_ok=True)
    frame.to_parquet(OUT_F, index=False)
    print("已写出 %s（%d 行）" % (OUT_F, len(frame)))
    summarize(frame)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
