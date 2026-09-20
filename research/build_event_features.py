# -*- coding: utf-8 -*-
"""build_event_features.py —— 事件级全特征表(B1+ 研究的数据基础)。
每个 B1 事件(T0)一行,T0 当天取值,覆盖用户口述特征树(板块除外,无数据):
量能: upvol_r(放量上涨) shrink(缩量回调) volr(当日量比) body_ratio(红长绿短)
位置: dist_ma20/60/120/240 悬空档 ma_align slope_ma20/60
结构: dist_hh60(回撤幅度) days_high60(回撤持续) 关键K(爆量阳线:存在/距今/破最低/破中点)
盈亏比: rr = 距60日高点空间 / 距20日低点风险
result(hit/miss/flat) 从 b1_event_stats 合并。
用法: python build_event_features.py   # → out/b1_event_features.parquet
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
from kdj.layers import baseline_original                                      # noqa: E402

KEYK_MULT = 3.0     # 爆量定义:量 ≥ 3×VMA20
KEYK_WIN = 60       # 关键K回看窗口


def event_features(df: pd.DataFrame, t: int) -> dict:
    o, h, l, c, v = (df[x].values for x in ("open", "high", "low", "close", "volume"))
    vma = df["vma20"].values
    f = {}
    # —— 量能(近20bar,与 case_features 同口径) ——
    w = slice(t - 19, t + 1)
    up = c[w] > o[w]
    uv, dv = v[w][up], v[w][~up]
    f["upvol_r"] = uv.mean() / dv.mean() if len(uv) and len(dv) and dv.mean() > 0 else np.nan
    body = np.abs(c[w] - o[w])
    f["body_ratio"] = body[up].mean() / body[~up].mean() if len(body[up]) and body[~up].mean() > 0 else np.nan
    m3 = df["volume"].rolling(3, min_periods=3).mean()
    f["shrink"] = (m3.iloc[t - 4:t + 1].min() / vma[t]
                   if t >= 4 and not np.isnan(m3.iloc[t]) else np.nan)
    f["volr"] = v[t] / vma[t]
    # —— 位置(全均线谱 + 下方最近均线,侦察"悬空"到底锚在哪条线) ——
    dists = {}
    for n in (5, 10, 20, 60, 120, 240):
        d = c[t] / df["ma%d" % n].values[t] - 1.0
        f["dist_ma%d" % n] = d
        dists[n] = d
    ma30 = df["close"].rolling(30, min_periods=30).mean().values[t]
    f["dist_ma30"] = c[t] / ma30 - 1.0 if not np.isnan(ma30) else np.nan
    dists[30] = f["dist_ma30"]
    pos = [d for d in dists.values() if d > 0]
    f["dist_nb"] = min(pos) if pos else np.nan        # 价格上方悬空度=距下方最近均线
    f["nb_ma"] = float(min(dists, key=lambda n: dists[n]) if pos else np.nan)  # 哪条线是最近支撑
    f["ma_align"] = float(df["ma5"].values[t] > df["ma20"].values[t] > df["ma60"].values[t])
    for n in (20, 60):
        a, b_ = df["ma%d" % n].values[t], df["ma%d" % n].values[t - 5]
        f["slope_ma%d" % n] = a / b_ - 1.0 if t >= 5 and not np.isnan(b_) else np.nan
    # —— 回撤幅度/持续时间 ——
    if t >= KEYK_WIN - 1:
        hw = h[t - KEYK_WIN + 1: t + 1]
        hh = hw.max()
        f["dist_hh60"] = c[t] / hh - 1.0
        f["days_high60"] = float(KEYK_WIN - 1 - int(np.argmax(hw)))
        # —— 关键K:近60bar 最近的爆量阳线 ——
        lo = t - KEYK_WIN + 1
        cand = np.nonzero((v[lo:t + 1] >= KEYK_MULT * vma[lo:t + 1]) & (c[lo:t + 1] > o[lo:t + 1]))[0]
        if len(cand):
            gi = lo + cand[-1]                       # 最近一个爆量阳线
            f["keyk"] = 1.0
            f["keyk_ago"] = float(t - gi)
            mid = (o[gi] + c[gi]) / 2.0
            f["keyk_brk_low"] = float(l[gi:t + 1].min() < l[gi])
            f["keyk_brk_mid"] = float(l[gi:t + 1].min() < mid)
        else:
            f.update(keyk=0.0, keyk_ago=np.nan, keyk_brk_low=np.nan, keyk_brk_mid=np.nan)
        # —— 盈亏比:到60日高点的空间 / 到20日低点的风险 ——
        ll20 = l[t - 19:t + 1].min()
        dn = 1.0 - ll20 / c[t]
        f["rr"] = (hh / c[t] - 1.0) / dn if dn > 0.01 else np.nan
    else:
        for k in ("dist_hh60", "days_high60", "keyk", "keyk_ago",
                  "keyk_brk_low", "keyk_brk_mid", "rr"):
            f[k] = np.nan
    return f


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
        for i, d in ep:
            r = {"sym": sym, "date": d, "board": board(sym)}
            r.update(event_features(df, i))
            rows.append(r)
        if k % 1000 == 0:
            print("[%d/%d] 事件累计 %d" % (k, len(syms), len(rows)))
    ev = pd.DataFrame(rows)
    stats = pd.read_parquet(os.path.join(OUT_DIR, "b1_event_stats.parquet"))
    ev = ev.merge(stats[["sym", "date", "result", "bull", "fwd15", "max_up", "max_dn"]],
                  on=["sym", "date"], how="left")
    out = os.path.join(OUT_DIR, "b1_event_features.parquet")
    ev.to_parquet(out)
    print("→ %s(%d 事件,%d 特征列)" % (out, len(ev), ev.shape[1]))
    return ev


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    build()
