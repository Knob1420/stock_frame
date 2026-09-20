# -*- coding: utf-8 -*-
"""event_stats.py —— 事件级统计(用户 2026-09-19 修正口径):
每个 B1 事件(episode 首日 T0)一行:入场=T0 次日开盘,窗口=入场后 15 个交易日,
记录 max_up(期间最高/入场-1)、max_dn(期间最低/入场-1)、fwd15;
C1–C5 在 T0 当天取值(事件层过滤,非时机层)。
用法: python event_stats.py   # → out/b1_event_stats.parquet + 对比统计
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
                             COOLDOWN, dedup_signals, load_bull, board)
from build_timing_days import day_conditions                                  # noqa: E402
from kdj.layers import baseline_original                                      # noqa: E402

H = 15   # 入场后观察窗口(交易日)
HIT, MISS = 0.08, -0.05   # 用户池口径:先到 +8%=hit,先到 −5%=miss


def first_touch(df: pd.DataFrame, i: int) -> str:
    """T0 次日开盘入场,15 bar 内先触 +8%(hit)/−5%(miss);均未触=flat。"""
    entry = df["open"].iloc[i + 1]
    for j in range(i + 1, min(i + 1 + H, len(df))):
        if df["low"].iloc[j] / entry - 1.0 <= MISS:
            return "miss"
        if df["high"].iloc[j] / entry - 1.0 >= HIT:
            return "hit"
    return "flat"


def build():
    bull = load_bull()
    syms = sorted(f[:-8] for f in os.listdir(IND_DIR)
                  if f.endswith(".parquet") and not f.startswith(INDEX_PREFIX))
    frames = []
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
        cond = day_conditions(df)
        entry = df["open"].shift(-1)                       # T0 次日开盘
        max_up = df["high"].shift(-1).rolling(H, min_periods=H).max().shift(-(H - 1)) / entry - 1.0
        max_dn = df["low"].shift(-1).rolling(H, min_periods=H).min().shift(-(H - 1)) / entry - 1.0
        fwd = df["close"].shift(-H) / entry - 1.0
        rows = []
        for i, d in ep:
            rows.append({"sym": sym, "date": d, "board": board(sym),
                         "bull": bool(bull.reindex([d], method="ffill").iloc[0]),
                         "entry": entry.iloc[i], "max_up": max_up.iloc[i],
                         "max_dn": max_dn.iloc[i], "fwd15": fwd.iloc[i],
                         "result": first_touch(df, i) if i + 1 + 1 < len(df) else "no_data",
                         **{c: bool(cond[c].iloc[i]) for c in ("c1", "c2", "c3", "c4", "c5", "combo")}})
        frames.append(pd.DataFrame(rows))
        if k % 1000 == 0:
            print("[%d/%d] 事件累计 %d" % (k, len(syms), sum(len(f_) for f_ in frames)))
    ev = pd.concat(frames, ignore_index=True)
    out = os.path.join(OUT_DIR, "b1_event_stats.parquet")
    ev.to_parquet(out)
    print("→ %s(%d 事件)\n" % (out, len(ev)))
    return ev


def report(ev: pd.DataFrame):
    def line(label, g):
        r = g.dropna(subset=["max_up"])
        if r.empty:
            print("%-26s n=0" % label)
            return
        hm = r["result"].isin(["hit", "miss", "flat"])
        p_hit = 100 * (r.loc[hm, "result"] == "hit").mean()
        print("%-26s n=%-7d hit率 %.1f%% | max_up 均值 %+.2f%% 中位 %+.2f%% P(≥8%%) %.1f%% | "
              "max_dn 均值 %+.2f%% P(≤−5%%) %.1f%% | fwd15 均值 %+.2f%%" % (
                  label, len(r), p_hit,
                  100 * r["max_up"].mean(), 100 * r["max_up"].median(), 100 * (r["max_up"] >= 0.08).mean(),
                  100 * r["max_dn"].mean(), 100 * (r["max_dn"] <= -0.05).mean(), 100 * r["fwd15"].mean()))

    print("== 事件级基线与 C1–C5 过滤对比(15 日窗口,T0 当天取条件) ==")
    line("全部事件", ev)
    for c in ("c1", "c2", "c3", "c4", "c5"):
        line("仅留 %s=True" % c, ev[ev[c]])
        line("仅留 %s=False" % c, ev[~ev[c]])
    line("combo=True(C1∧C2∧C3∧C4)", ev[ev["combo"]])
    line("combo=False", ev[~ev["combo"]])
    print()
    print("== combo=True 分年(fwd15 均值/max_up 中位) ==")
    ev2 = ev.dropna(subset=["max_up"]).copy()
    ev2["year"] = pd.to_datetime(ev2["date"]).dt.year
    g = ev2.groupby("year").apply(lambda x: pd.Series({
        "n": len(x), "n_combo": int(x["combo"].sum()),
        "all_fwd15": 100 * x["fwd15"].mean(), "all_up_med": 100 * x["max_up"].median(),
        "cb_fwd15": 100 * x.loc[x["combo"], "fwd15"].mean(),
        "cb_up_med": 100 * x.loc[x["combo"], "max_up"].median()}), include_groups=False)
    print(g.to_string(float_format=lambda x: "%.1f" % x))


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    report(build())
