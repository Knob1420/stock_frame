# -*- coding: utf-8 -*-
"""build_b1_events.py —— Phase 1:B1(原公式复刻)历史事件库。
数据面:stockdata/indicators/*.parquet(零 qlib 依赖);B1 = baseline_original 口径
(GOOD & J<=15 & |PCT|<=3% & AMP<=7%,kdj/layers.py 复用)。
每个信号一行事件:入场(次日开盘,涨停/停牌标 void)、前向 5/10/20 日结局、
MFE/MAE(10日)、T+5 含成本收益、牛熊标签(沪深300 close>MA240)。
同时流式累计全池无条件前向收益作对照基准(同口径:次日开盘入场)。
用法:
  python build_b1_events.py            # 全池,写 out/b1_events.parquet + out/b1_baseline.md
  python build_b1_events.py --syms sh600000,sz000651   # 小样调试
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from kdj.layers import baseline_original          # noqa: E402  纯函数复用
from kdj.backtest import Costs, LIMIT_UP          # noqa: E402  口径复用

IND_DIR = os.path.join(ROOT, "stockdata", "indicators")
OUT_DIR = os.path.join(HERE, "out")
INDEX_PREFIX = ("sh00", "sz39")                   # 指数族剔除;其余全市场(主板/创业板/科创板/北交所)
MIN_BARS = 120                                    # 剔次新
COOLDOWN = 5                                      # 事件去重:信号日间隔≤5交易日(J 钝化连发)合并为一个事件,取首日
START = "2010-01-01"                              # 事件/基准仅记录 2010 起;信号在全历史上算(EMA/长均线预热)
FWD_HORIZONS = (5, 10, 20)
COSTS = Costs()


def board(sym: str) -> str:
    """板块标签:主板/创业板/科创板/北交所(分析期按板块切片,交易只做主板)。"""
    if sym.startswith(("sh60", "sz00")):
        return "主板"
    if sym.startswith("sz30"):
        return "创业板"
    if sym.startswith("sh68"):
        return "科创板"
    return "北交所"


def load_bull() -> pd.Series:
    """沪深300 close>MA240 布尔标签,index=DatetimeIndex。"""
    df = pd.read_parquet(os.path.join(IND_DIR, "sh000300.parquet"))
    s = df["close"]
    s.index = pd.DatetimeIndex(s.index)
    ma = s.rolling(240, min_periods=240).mean()
    return (s > ma).fillna(False)


def fwd_vec(df: pd.DataFrame, n: int) -> pd.Series:
    """前向 n 日收益(次日开盘入场→第 n 日收盘),全 bar 向量化;末 n bar 为 NaN。"""
    return df["close"].shift(-n) / df["open"].shift(-1) - 1.0


def dedup_signals(sig: pd.Series, cooldown: int = COOLDOWN) -> pd.Series:
    """连续/邻近触发合并为一个事件:距上一保留信号 ≤cooldown 交易日的触发并入同簇。
    返回仅保留每簇首日的布尔列(episode 口径,用户 2026-09-18 确认)。"""
    idxs = sig.values.nonzero()[0]
    kept = set()
    last = -10**9
    for i in idxs:
        if i - last > cooldown:
            kept.add(i)
            last = i
    out = pd.Series(False, index=sig.index)
    out.iloc[sorted(kept)] = True
    return out


def process_stock(sym: str, bull: pd.Series):
    """返回 (events DataFrame, 全池基准累积行 dict 列表)。"""
    df = pd.read_parquet(os.path.join(IND_DIR, "%s.parquet" % sym))
    df = df.dropna(subset=["close"])
    if len(df) < MIN_BARS:
        return pd.DataFrame(), pd.DataFrame()
    df.index = pd.DatetimeIndex(df.index)
    df["j"] = df["kdj_j"]

    # 无条件基准(全 bar,只取 fwd10):向量化构造,避免逐行 dict
    f10 = fwd_vec(df, 10)
    f10 = f10[f10.index >= START].dropna()
    b = bull.reindex(f10.index, method="ffill").fillna(False)
    bench = pd.DataFrame({"sym": sym, "board": board(sym), "date": f10.index,
                          "fwd10": f10.values, "bull": b.values})

    sig = dedup_signals(baseline_original(df))
    sig_dates = sig.index[sig & (sig.index >= START)]
    events = []
    for d in sig_dates:
        i = df.index.get_loc(d)
        if i + 1 >= len(df):
            events.append({"sym": sym, "signal_date": d, "reason": "data_end"})
            continue
        nxt = df.iloc[i + 1]
        entry = float(nxt["open"])
        if np.isnan(entry):
            events.append({"sym": sym, "signal_date": d, "reason": "suspended"})
            continue
        if entry >= df["close"].iloc[i] * LIMIT_UP:
            events.append({"sym": sym, "signal_date": d, "reason": "limit_up"})
            continue
        e = {"sym": sym, "signal_date": d, "reason": "", "entry_date": df.index[i + 1],
             "entry": entry, "gap_days": (df.index[i + 1] - d).days,
             "censored": False,
             "bull": bool(bull.reindex([d], method="ffill").iloc[0])
             if (bull.index <= d).any() else False}
        ei = i + 1                                    # 入日位置
        for n in FWD_HORIZONS:
            k = ei + n - 1
            if k < len(df):
                e["fwd%d" % n] = float(df["close"].iloc[k]) / entry - 1.0
            else:
                e["fwd%d" % n] = np.nan
                e["censored"] = True
        seg = df.iloc[ei: ei + 10]
        e["mfe10"] = float(seg["high"].max() / entry - 1.0)
        e["mae10"] = float(seg["low"].min() / entry - 1.0)
        k = ei + 4                                    # T+5(入日+4 收盘卖),与 e1 对齐
        e["t5_cost"] = (float(df["close"].iloc[min(k, len(df) - 1)]) * COSTS.sell_mult()
                        / (entry * COSTS.buy_mult()) - 1.0) if k < len(df) else np.nan
        events.append(e)
    ev = pd.DataFrame(events)
    if not ev.empty:
        ev["board"] = board(sym)
    return ev, bench


def summarize(events: pd.DataFrame, bench: pd.DataFrame) -> str:
    """B1 vs 无条件基准:总体 + 年×牛熊。"""
    ok = events[events["reason"] == ""].copy() if not events.empty else events
    lines = []

    def stats_line(label, ret, extra=""):
        if ret is None:
            return "| %s | 0 | - | - | - |" % label
        r = ret.dropna()
        if r.empty:
            return "| %s | 0 | - | - | - |" % label
        return "| %s | %d | %.1f%% | %.2f%% |%.2f%% |" % (
            label, len(r), 100 * (r > 0).mean(), 100 * r.mean(), 100 * r.median())

    lines += ["## 总体(信号 n=%d,可入場 n=%d,买不进率 %.1f%%)" % (
        len(events), len(ok), 100 * (events["reason"] != "").mean() if len(events) else 0),
        "| 口径 | n | 上涨概率 | 均值 | 中位数 |", "|---|---|---|---|---|"]
    for n in FWD_HORIZONS:
        lines.append(stats_line("B1 前向%d日" % n, ok.get("fwd%d" % n)))
    lines.append(stats_line("B1 T+5 含成本", ok.get("t5_cost")))
    lines.append(stats_line("全池无条件 前向10日", bench["fwd10"]))
    lines.append(stats_line("全池无条件 前向10日(牛市)", bench.loc[bench["bull"], "fwd10"]))
    lines.append(stats_line("全池无条件 前向10日(熊市)", bench.loc[~bench["bull"], "fwd10"]))
    lines.append("")
    lines += ["## B1 前向10日:分年×牛熊", "| 年 | 牛熊 | n | 上涨概率 | 均值 |", "|---|---|---|---|---|"]
    if not ok.empty:
        ok["year"] = pd.to_datetime(ok["signal_date"]).dt.year
        for (y, b), g in ok.groupby(["year", "bull"]):
            r = g["fwd10"].dropna()
            if len(r):
                lines.append("| %d | %s | %d | %.1f%% | %.2f%% |" % (
                    y, "牛" if b else "熊", len(r), 100 * (r > 0).mean(), 100 * r.mean()))
    lines += ["", "## B1 前向10日:分板块×牛熊", "| 板块 | 牛熊 | n | 上涨概率 | 均值 |", "|---|---|---|---|---|"]
    if not ok.empty:
        for (b_, bull_), g in ok.groupby([ok.get("board"), "bull"]):
            r = g["fwd10"].dropna()
            if len(r):
                lines.append("| %s | %s | %d | %.1f%% | %.2f%% |" % (
                    b_, "牛" if bull_ else "熊", len(r), 100 * (r > 0).mean(), 100 * r.mean()))
    lines += ["", "## 摩擦", "| 原因 | n |", "|---|---|"]
    if len(events):
        for rsn, n in events[events["reason"] != ""]["reason"].value_counts().items():
            lines.append("| %s | %d |" % (rsn, n))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--syms", default=None, help="逗号分隔,调试用;缺省全主板池")
    a = ap.parse_args(argv)

    os.makedirs(OUT_DIR, exist_ok=True)
    bull = load_bull()
    if a.syms:
        syms = [s.strip().lower() for s in a.syms.split(",")]
    else:
        syms = sorted(f[:-8] for f in os.listdir(IND_DIR)
                      if f.endswith(".parquet") and not f.startswith(INDEX_PREFIX))

    all_events, all_bench = [], []
    for k, sym in enumerate(syms, 1):
        try:
            ev, bench = process_stock(sym, bull)
        except Exception as e:                       # 单票异常不炸整体
            print("[%d/%d] %s 失败: %s" % (k, len(syms), sym, e))
            continue
        all_events.append(ev)
        all_bench.append(bench)
        if k % 500 == 0:
            print("[%d/%d] 累计事件 %d" % (k, len(syms), sum(len(e) for e in all_events)))

    events = pd.concat(all_events, ignore_index=True) if all_events else pd.DataFrame()
    bench = pd.concat(all_bench, ignore_index=True) if all_bench else pd.DataFrame()
    events.to_parquet(os.path.join(OUT_DIR, "b1_events.parquet"))
    bench.to_parquet(os.path.join(OUT_DIR, "b1_benchmark.parquet"))
    md = ("# B1(原公式)基线报告\n\n> B1 = GOOD & J<=15 & |PCT|<=3%% & AMP<=7%%;入场=次日开盘(涨停/停牌 void);\n"
          "> 前向 N 日 = 次日开盘→第 N 日收盘;成本口径万2.5佣金+千1印花+千1滑点(T+5 列)。\n\n%s\n" % summarize(
        events, bench))
    with open(os.path.join(OUT_DIR, "b1_baseline.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    print("事件 %d 条 → %s" % (len(events), os.path.join(OUT_DIR, "b1_events.parquet")))


if __name__ == "__main__":
    main()
