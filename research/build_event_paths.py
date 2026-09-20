# -*- coding: utf-8 -*-
"""build_event_paths.py —— 事件路径表:39.3 万 B1 事件 × 入场后 15 个交易日,逐日记录。
每行 = (事件, 第d日): 相对入场的 open/close/high/low 涨跌、量比、截至当日累计新高/新低、
触发日(hit/miss 先到日)。支持:路径形态统计、持有节奏、MFE/MAE 演化、"等回踩"模拟。
用法: python build_event_paths.py   # → out/b1_event_paths.parquet + 形态/节奏统计
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from build_b1_events import (IND_DIR, OUT_DIR, START, INDEX_PREFIX, MIN_BARS,  # noqa: E402
                             dedup_signals, load_bull, board)
from event_stats import H, HIT, MISS                                          # noqa: E402
from kdj.layers import baseline_original                                      # noqa: E402

COLS = ["sym", "ep_date", "date", "offset", "ret_open", "ret_close", "hi", "lo",
        "volr", "cum_up", "cum_dn", "result", "trig_day", "bull", "board"]


def build():
    bull = load_bull()
    syms = sorted(f[:-8] for f in os.listdir(IND_DIR)
                  if f.endswith(".parquet") and not f.startswith(INDEX_PREFIX))
    rows = []
    for k, sym in enumerate(syms, 1):
        try:
            df = pd.read_parquet(os.path.join(IND_DIR, "%s.parquet" % sym))
        except Exception as e:
            print("%s 读取失败: %s" % (sym, e))
            continue
        df = df.dropna(subset=["close"])
        if len(df) < MIN_BARS:
            continue
        df.index = pd.DatetimeIndex(df.index)
        sig = dedup_signals(baseline_original(df.assign(j=df["kdj_j"])))
        ep = [(i, d) for i, d in zip(np.nonzero(sig.values)[0], sig.index[sig])
              if d >= pd.Timestamp(START)]
        if not ep:
            continue
        o, h, l, c, v = (df[x].values for x in ("open", "high", "low", "close", "volume"))
        vma = df["vma20"].values
        idx = df.index
        bd = board(sym)
        for i, d in ep:
            if i + 2 >= len(df) or np.isnan(o[i + 1]):
                continue
            entry = o[i + 1]
            bull_i = bool(bull.reindex([d], method="ffill").iloc[0])
            # 先触 hit/miss 的日 offset
            res, trig = "flat", H
            for j in range(i + 1, min(i + 1 + H, len(df))):
                off = j - i
                if l[j] / entry - 1.0 <= MISS:
                    res, trig = "miss", off
                    break
                if h[j] / entry - 1.0 >= HIT:
                    res, trig = "hit", off
                    break
            cu, cd = -np.inf, np.inf
            for j in range(i + 1, min(i + 1 + H, len(df))):
                off = j - i
                cu = max(cu, h[j] / entry - 1.0)
                cd = min(cd, l[j] / entry - 1.0)
                rows.append((sym, d, idx[j], off, o[j] / entry - 1.0, c[j] / entry - 1.0,
                             h[j] / entry - 1.0, l[j] / entry - 1.0,
                             v[j] / vma[j] if not np.isnan(vma[j]) else np.nan,
                             cu, cd, res, trig, bull_i, bd))
        if k % 1000 == 0:
            print("[%d/%d] 路径累计 %d 行" % (k, len(syms), len(rows)))
    p = pd.DataFrame(rows, columns=COLS)
    out = os.path.join(OUT_DIR, "b1_event_paths.parquet")
    p.to_parquet(out)
    print("→ %s(%d 行)\n" % (out, len(p)))
    return p


def report(p: pd.DataFrame):
    ev = p.groupby(["sym", "ep_date"]).tail(1)[["sym", "ep_date", "result", "trig_day", "bull"]]
    print("== 触发节奏 ==")
    for r_ in ("hit", "miss"):
        s = ev.loc[ev.result == r_, "trig_day"]
        print("%-5s n=%-7d 中位触发日 %s | P(≤3日) %.0f%% P(≤5日) %.0f%% P(≤8日) %.0f%%" % (
            r_, len(s), s.median(), 100 * (s <= 3).mean(), 100 * (s <= 5).mean(),
            100 * (s <= 8).mean()))
    print()
    print("== 路径形态(触发前的反向运动) ==")
    for r_ in ("hit", "miss"):
        g = p[(p.result == r_) & (p.offset < p.trig_day)]
        if r_ == "hit":
            rev = g.groupby(["sym", "ep_date"])["lo"].min()      # 触发前最深回撤
            print("hit 前回撤:中位 %+.1f%% | P(≤−3%%) %.0f%% | P(≤−5%%) %.0f%%" % (
                100 * rev.median(), 100 * (rev <= -0.03).mean(), 100 * (rev <= -0.05).mean()))
        else:
            rev = g.groupby(["sym", "ep_date"])["hi"].max()      # 触发前最大反抽
            print("miss 前反抽:中位 %+.1f%% | P(≥+3%%) %.0f%% | P(≥+5%%) %.0f%%" % (
                100 * rev.median(), 100 * (rev >= 0.03).mean(), 100 * (rev >= 0.05).mean()))
    print()
    print("== MFE/MAE 演化(均值,按日) ==")
    g = p.groupby("offset")[["cum_up", "cum_dn", "ret_close"]].mean()
    print(g.to_string(float_format=lambda x: "%+.2f%%" % (100 * x)))


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    report(build())
