# -*- coding: utf-8 -*-
"""entry_timing.py —— 入场时机专题(用户 2026-09-19 提问):
① 固定延迟:T+1 直接买 vs 等 1-5 日
② J 值分档:T0 的 J ∈ [10,15) / [0,10) / <0 胜率差异
③ 等 J 抬头:首次 J 上拐日次日入场 vs 直接入场
结局口径:fwd15(入场=该策略入场日次日开盘?不——统一:入场=指定日开盘,持到+15 交易日收盘)。
用法: python entry_timing.py   # → out/b1_entry_timing.parquet + 统计
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
                             dedup_signals, board)
from kdj.layers import baseline_original                                      # noqa: E402

MAX_DELAY = 5      # 固定延迟档位
WAIT_WIN = 10      # 等 J 抬头的最长等待窗口(交易日)


def build():
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
        o, c, j = df["open"].values, df["close"].values, df["kdj_j"].values
        n = len(df)
        for i, d in ep:
            if i + 17 >= n or np.isnan(o[i + 1]):
                continue
            r = {"sym": sym, "date": d, "board": board(sym), "j_t0": j[i]}
            # ① 固定延迟 d 日入场(delay=1 即 T+1 直接入场)
            for dl in range(1, MAX_DELAY + 1):
                ent = i + dl
                r["fwd15_d%d" % dl] = c[ent + 15] / o[ent] - 1.0 if ent + 15 < n else np.nan
            # ③ 等 J 抬头:窗口内首个 j[x] > j[x-1] 的 x,次日开盘入场(+15 收盘)
            up = next((x for x in range(i + 1, min(i + 1 + WAIT_WIN, n))
                       if j[x] > j[x - 1]), None)
            r["jup_day"] = up - i if up is not None else np.nan
            ent = up + 1 if up is not None and up + 16 < n else None
            r["fwd15_jup"] = c[ent + 15] / o[ent] - 1.0 if ent is not None else np.nan
            rows.append(r)
        if k % 1000 == 0:
            print("[%d/%d] %d 事件" % (k, len(syms), len(rows)))
    t = pd.DataFrame(rows)
    out = os.path.join(OUT_DIR, "b1_entry_timing.parquet")
    t.to_parquet(out)
    print("→ %s(%d)\n" % (out, len(t)))
    return t


def report(t: pd.DataFrame):
    def line(lab, s):
        s = s.dropna()
        print("%-24s n=%-7d 胜率 %.1f%% 均值 %+.2f%% 中位 %+.2f%%" % (
            lab, len(s), 100 * (s > 0).mean(), 100 * s.mean(), 100 * s.median()))
    print("== ① 固定延迟(fwd15) ==")
    for dl in range(1, MAX_DELAY + 1):
        line("T+%d 入场" % dl, t["fwd15_d%d" % dl])
    print()
    print("== ② T0 时 J 值分档(T+1 入场,fwd15) ==")
    d1 = t["fwd15_d1"]
    for lab, m in [("J∈[10,15)", (t.j_t0 >= 10) & (t.j_t0 < 15)),
                   ("J∈[0,10)", (t.j_t0 >= 0) & (t.j_t0 < 10)),
                   ("J<0", t.j_t0 < 0)]:
        line(lab, d1[m])
    print()
    print("== ③ 等 J 抬头 ==")
    line("直接 T+1", d1)
    line("等J抬头(窗口内)", t["fwd15_jup"])
    jd = t["jup_day"].dropna()
    print("抬头日分布:中位 %.0f | P(当日=1) %.0f%% P(≤3日) %.0f%% 窗口内无 %.0f%%" % (
        jd.median(), 100 * (jd == 1).mean(), 100 * (jd <= 3).mean(),
        100 * t["jup_day"].isna().mean()))
    line("抬头早(≤2日)入场", t.loc[t.jup_day <= 2, "fwd15_jup"])
    line("抬头晚(≥5日)入场", t.loc[t.jup_day >= 5, "fwd15_jup"])


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    report(build())
