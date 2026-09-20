# -*- coding: utf-8 -*-
"""path_archetypes.py —— 门内 B1+ 路径原型与分叉点(用户 P3)。
数据:b1_event_paths(15日逐日)+ b1_event_features(B1+ 过滤)+ market_state(环境门)。
原型(互斥,按序判):
  直接启动   前3日 cum_up≥+5% 且结局 hit
  冲高回落   前5日 cum_up≥+5% 但 fwd15<0
  先跌后涨   前5日 lo≤−3% 且结局 hit
  横盘后涨   前端无动作(|ret_close|<±2%)且 hit
  持续阴跌   前5日 lo≤−3% 且 miss
  高抛低吸未遂/其余 → 其他
分叉点:第k日检查点(cum_up 分档)× P(hit)/fwd15,k=1..7 —— 最早哪天"大局已定"。
用法: python path_archetypes.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")

f = pd.read_parquet(os.path.join(OUT, "b1_event_features.parquet"))
ms = pd.read_parquet(os.path.join(OUT, "market_state.parquet"))
b1p = f[(f.dist_ma60 > 0.06) & (f.slope_ma20 > 0.02) & (f.keyk == 1) & (f.keyk_brk_low == 0)]
b1p = b1p.join(ms[["above_ma60", "idx_slope20"]], on=pd.to_datetime(b1p.date))
gate = b1p[(b1p.above_ma60 > 0.6) & (b1p.idx_slope20 > 0)][["sym", "date"]]

p = pd.read_parquet(os.path.join(OUT, "b1_event_paths.parquet"))
m = p.merge(gate, left_on=["sym", "ep_date"], right_on=["sym", "date"], how="inner")

rows = []
for (sym, ep), g in m.groupby(["sym", "ep_date"]):
    g = g.sort_values("offset")
    d = {int(r.offset): r for r in g.itertuples()}
    if 15 not in d:
        continue
    early_up3 = max(d[k].cum_up for k in (1, 2, 3) if k in d) >= 0.05
    lo5 = min(d[k].lo for k in range(1, 6) if k in d)
    early_dn = lo5 <= -0.03
    res, fwd = d[15].result, d[15].ret_close
    if early_up3 and res == "hit":
        a = "直接启动"
    elif max(d[k].cum_up for k in range(1, 6) if k in d) >= 0.05 and fwd < 0:
        a = "冲高回落"
    elif early_dn and res == "hit":
        a = "先跌后涨"
    elif not early_up3 and not early_dn and res == "hit":
        a = "横盘后涨"
    elif res == "miss":
        a = "持续阴跌" if early_dn else "无声阴跌"
    else:
        a = "其他"
    rows.append({"sym": sym, "ep": ep, "arch": a, "result": res, "fwd15": fwd,
                 "trig": d[15].trig_day, "max_up": max(r.cum_up for r in d.values()),
                 "max_dn": min(r.cum_dn for r in d.values())})
A = pd.DataFrame(rows)
print("门内 B1+ 事件 n=%d\n" % len(A))

print("== 原型分布与结局 ==")
g = A.groupby("arch").agg(n=("fwd15", "size"), fwd15均=("fwd15", lambda x: 100 * x.mean()),
                          fwd15中位=("fwd15", lambda x: 100 * x.median()),
                          hit率=("result", lambda x: 100 * (x == "hit").mean()),
                          miss率=("result", lambda x: 100 * (x == "miss").mean()),
                          max_up中位=("max_up", lambda x: 100 * x.median()),
                          max_dn中位=("max_dn", lambda x: 100 * x.median()))
g["占比%"] = 100 * g.n / len(A)
print(g.to_string(float_format=lambda x: "%.1f" % x))

print("\n== 分叉点:第k日 cum_up 档 × 结局(k=1..7) ==")
for k in range(1, 8):
    cu, ok = [], []
    for (sym, ep), gg in m.groupby(["sym", "ep_date"]):
        r = gg[gg.offset == k]
        if len(r) and 15 in set(gg.offset):
            cu.append(float(r.cum_up.iloc[0]))
            ok.append((sym, ep))
    if not cu:
        continue
    s = pd.Series(cu, index=pd.MultiIndex.from_tuples(ok, names=["sym", "ep"]))
    lab = pd.cut(s, [-1, -0.03, 0.03, 0.08, 9], labels=["≤−3%", "±3%", "+3~8%", ">+8%"])
    j = pd.DataFrame({"cu": lab}).join(A.set_index(["sym", "ep"])[["result", "fwd15"]])
    g2 = j.groupby("cu", observed=True).agg(n=("fwd15", "size"),
                                            hit=("result", lambda x: 100 * (x == "hit").mean()),
                                            miss=("result", lambda x: 100 * (x == "miss").mean()),
                                            fwd15=("fwd15", lambda x: 100 * x.mean()))
    print("\n-- 第 %d 日收盘时累计涨幅 --" % k)
    print(g2.to_string(float_format=lambda x: "%.1f" % x))
