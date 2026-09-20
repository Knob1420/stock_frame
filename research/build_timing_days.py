# -*- coding: utf-8 -*-
"""build_timing_days.py —— Phase 3 时机层:事件窗口内日级裁决。
对每个 B1 事件(episode 首日 T0),取窗口 [T0, T0+10] 的每个交易日:
记录 C1–C5 时机条件(完美案例提炼,只作假设)与前向10日结局(次日开盘入场)。
裁决问题:B1 响了之后,"等到 C1∧C2∧C3∧C4 的日子"进场,是否优于窗口内其他日子/首触发日?
用法:
  python build_timing_days.py            # 全市场 → out/b1_timing_days.parquet
  python build_timing_days.py --analyze  # 只分析已有 parquet
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

from build_b1_events import (IND_DIR, OUT_DIR, START, INDEX_PREFIX, MIN_BARS,  # noqa: E402
                             COOLDOWN, dedup_signals, load_bull, board)
from kdj.layers import baseline_original                                       # noqa: E402

WINDOW = 10          # 事件后可等待的交易日窗口
SHRINK_THR = 0.73    # C2 阈值(完美案例最松值;连续值已存,阈值可后扫)
C4_THR = -0.09       # C4 距 60 日高点回撤下限
C5_THR = 0.85        # C5 信号日缩量上限


def day_conditions(df: pd.DataFrame) -> pd.DataFrame:
    """向量化日级条件列(全历史算,2010 起由窗口过滤)。"""
    up = df["close"] > df["open"]
    # 窗口内上涨日均量/下跌日均量:sum/count 口径(与 case_features 一致;where+mean 会因 NaN 凑不满窗口)
    upv = df["volume"].where(up).rolling(20, min_periods=1).sum() / up.rolling(20, min_periods=1).sum()
    dnv = df["volume"].where(~up).rolling(20, min_periods=1).sum() / (~up).rolling(20, min_periods=1).sum()
    m3 = df["volume"].rolling(3, min_periods=3).mean()
    shrink = m3.rolling(5, min_periods=1).min() / df["vma20"]
    hh60 = df["high"].rolling(60, min_periods=60).max()
    out = pd.DataFrame(index=df.index)
    out["upvol_r"] = upv / dnv
    out["shrink"] = shrink
    out["dist_ma60"] = df["close"] / df["ma60"] - 1.0
    out["dist_hh60"] = df["close"] / hh60 - 1.0
    out["volr"] = df["volume"] / df["vma20"]
    out["c1"] = out["upvol_r"] >= 1.0
    out["c2"] = shrink < SHRINK_THR
    out["c3"] = out["dist_ma60"] > 0
    out["c4"] = out["dist_hh60"] <= C4_THR
    out["c5"] = out["volr"] < C5_THR
    out["combo"] = out["c1"] & out["c2"] & out["c3"] & out["c4"]
    return out


def build_days():
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
        ep_pos = [i for i, d in zip(np.nonzero(sig.values)[0], sig.index[sig])
                  if d >= pd.Timestamp(START)]
        if not ep_pos:
            continue
        cond = day_conditions(df)
        fwd = (df["close"].shift(-10) / df["open"].shift(-1) - 1.0).values   # 任意日入场前向10日
        cb = {c: cond[c].values for c in ("combo", "c1", "c2", "c3", "c4", "c5",
                                          "upvol_r", "shrink", "dist_ma60", "dist_hh60", "volr")}
        idx = df.index
        bd = board(sym)
        rows = []
        for i0 in ep_pos:
            bull_i = bool(bull.reindex([idx[i0]], method="ffill").iloc[0])
            for i in range(i0, min(i0 + WINDOW + 1, len(df) - 10)):
                rows.append({"sym": sym, "ep_date": idx[i0], "date": idx[i],
                             "offset": i - i0, "fwd10": fwd[i],
                             "combo": bool(cb["combo"][i]),
                             "c1": bool(cb["c1"][i]), "c2": bool(cb["c2"][i]),
                             "c3": bool(cb["c3"][i]), "c4": bool(cb["c4"][i]),
                             "c5": bool(cb["c5"][i]),
                             "upvol_r": cb["upvol_r"][i], "shrink": cb["shrink"][i],
                             "dist_ma60": cb["dist_ma60"][i],
                             "dist_hh60": cb["dist_hh60"][i], "volr": cb["volr"][i],
                             "bull": bull_i, "board": bd})
        frames.append(pd.DataFrame(rows))
        if k % 500 == 0:
            print("[%d/%d] 事件窗累计 %d 行" % (k, len(syms), sum(len(f_) for f_ in frames)))
    days = pd.concat(frames, ignore_index=True)
    out = os.path.join(OUT_DIR, "b1_timing_days.parquet")
    days.to_parquet(out)
    print("→ %s(%d 行)" % (out, len(days)))


def analyze():
    days = pd.read_parquet(os.path.join(OUT_DIR, "b1_timing_days.parquet"))
    r = days["fwd10"].dropna()
    print("n=%d 日级样本(%d 事件窗)" % (len(r), days["ep_date"].nunique()))

    def line(label, s):
        s = s.dropna()
        print("%-28s n=%-9d 胜率 %.1f%%  均值 %+.2f%%  中位 %+.2f%%" % (
            label, len(s), 100 * (s > 0).mean(), 100 * s.mean(), 100 * s.median()))

    line("窗口内全部日(对照)", r)
    line("首触发日(offset=0)", days.loc[days["offset"] == 0, "fwd10"])
    line("combo=True 日", days.loc[days["combo"], "fwd10"])
    line("combo=False 日", days.loc[~days["combo"], "fwd10"])
    print()
    for c in ("c1", "c2", "c3", "c4", "c5"):
        line("%s=True" % c, days.loc[days[c], "fwd10"])
        line("%s=False" % c, days.loc[~days[c], "fwd10"])
    print()
    print("combo 频率:窗口内 %.1f%% 的日;%.1f%% 的事件窗至少含 1 个 combo 日" % (
        100 * days["combo"].mean(),
        100 * days.groupby("ep_date")["combo"].any().mean()))
    print()
    print("combo 日 vs 同窗非 combo 日,分年×牛熊(均值差,百分比点):")
    days["year"] = pd.to_datetime(days["date"]).dt.year
    g = days.groupby(["year", "bull"])[["combo", "fwd10"]].apply(lambda x: pd.Series({
        "n_combo": int(x["combo"].sum()),
        "combo_mean": x.loc[x["combo"], "fwd10"].mean(),
        "other_mean": x.loc[~x["combo"], "fwd10"].mean()}))
    g["diff"] = g["combo_mean"] - g["other_mean"]
    g = g[g["n_combo"] >= 100]
    print(g.to_string(float_format=lambda x: "%.2f" % x))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyze", action="store_true")
    a = ap.parse_args(argv)
    if a.analyze:
        analyze()
    else:
        os.makedirs(OUT_DIR, exist_ok=True)
        build_days()
        analyze()


if __name__ == "__main__":
    main()
