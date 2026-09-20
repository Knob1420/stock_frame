# -*- coding: utf-8 -*-
"""b2_verify.py —— B2 独立复核回测器(用户 2026-09-20 口径,零复用 research/ 代码)。
不 import 本项目 research/ 任何模块;只用 stockdata/indicators 原始列。
信号:B1 = EMA2>EMA60(短线金叉长线) & kdj_j<=15 & |涨跌|<=3% & 振幅<=7%;
      B1V = B1 & 红长绿短(body_ratio>1.2) & 放量上涨(upvol_r>=1) & 缩量回调(shrink<0.8);
      环境门读 market_state.parquet(宽度>60% & 指数MA20上行)——唯一依赖旧产物,披露。
模拟:T+1 开盘入场(开盘>=昨收*1.097 或 NaN → 无法成交);
      15 根 K 线内:触及 -5% 止损 / +15% 止盈(按触价成交,同日双触按止损) / 第15日收盘离场。
输出:每信号 [收益, MFE, MAE, 持有天数, 离场原因];汇总 B1/B1V/B1V门内 × 分年。
用法: python b2_verify.py [--csv out/b2_trades.csv]
"""
import argparse
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
IND = os.path.join(os.path.dirname(HERE), "stockdata", "indicators")
OUT = os.path.join(HERE, "out")

SL, TP, HOLD, COST = -0.05, 0.15, 15, 0.0035


def signal_and_features(df):
    """独立重算:B1 与量能条件(向量化,不复用旧实现)。"""
    c, o, v = df["close"], df["open"], df["volume"]
    good = c.ewm(span=2, adjust=False).mean() > c.ewm(span=60, adjust=False).mean()
    pct = c.pct_change()
    amp = (df["high"] - df["low"]) / c.shift(1)
    b1 = good & (df["kdj_j"] <= 15) & (pct.abs() <= 0.03) & (amp <= 0.07)

    up = (c > o).astype(float)
    w = 20
    upv = v.where(up > 0).rolling(w, min_periods=1).sum() / up.rolling(w, min_periods=1).sum()
    dnv = v.where(up == 0).rolling(w, min_periods=1).sum() / (1 - up).rolling(w, min_periods=1).sum()
    upvol_r = upv / dnv                                   # 放量上涨:上涨日均量/下跌日均量
    m3 = v.rolling(3, min_periods=3).mean()
    shrink = m3.rolling(5, min_periods=1).min() / df["vma20"]   # 缩量回调:近5日最低3日均量/VMA20
    body = (c - o).abs()
    body_ratio = (body.where(up > 0).rolling(w, min_periods=1).sum() / up.rolling(w, min_periods=1).sum()
                  ) / (body.where(up == 0).rolling(w, min_periods=1).sum() / (1 - up).rolling(w, min_periods=1).sum())
    vol_ok = (body_ratio > 1.2) & (upvol_r >= 1.0) & (shrink < 0.8)

    # B1+:悬空 + MA20 斜率 + 关键K(爆量阳线)未破(独立实现)
    dist60 = c / df["ma60"] - 1
    slope20 = df["ma20"] / df["ma20"].shift(5) - 1
    blast = ((v >= 3 * df["vma20"]) & (c > o)).to_numpy()
    lows = df["low"].to_numpy()
    bi = np.nonzero(blast)[0]
    unbroken = np.zeros(len(df), dtype=bool)
    j = -1
    for i in range(len(df)):
        while j + 1 < len(bi) and bi[j + 1] <= i:
            j += 1
        if j >= 0 and i - bi[j] < 60 and lows[bi[j]:i + 1].min() >= lows[bi[j]]:
            unbroken[i] = True
    return b1, vol_ok, (dist60 > 0.06) & (slope20 > 0.02) & unbroken


def simulate(df, i):
    """T0=i → 返回 dict 或 None(无法成交)。"""
    if i + 1 >= len(df):
        return None
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    entry = o[i + 1]
    if np.isnan(entry) or entry >= c[i] * 1.097:
        return None
    mfe, mae = 0.0, 0.0
    for k in range(i + 1, min(i + 1 + HOLD, len(df))):
        mfe = max(mfe, h[k] / entry - 1)
        mae = min(mae, l[k] / entry - 1)
        if l[k] / entry - 1 <= SL:
            gross, why = SL, "止损"
            break
        if h[k] / entry - 1 >= TP:
            gross, why = TP, "止盈"
            break
    else:
        k = min(i + HOLD, len(df) - 1)
        gross, why = c[k] / entry - 1, "到期"
    return {"ret_gross": gross, "ret_net": (1 + gross) * (1 - COST) - 1,
            "mfe": mfe, "mae": mae, "hold": k - i, "why": why}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None)
    a = ap.parse_args(argv)
    ms = pd.read_parquet(os.path.join(OUT, "market_state.parquet"))
    gate = (ms["above_ma60"] > 0.6) & (ms["idx_slope20"] > 0)

    rows = []
    for fn in sorted(os.listdir(IND)):
        if not fn.endswith(".parquet") or fn.startswith(("sh00", "sz39")):
            continue
        df = pd.read_parquet(os.path.join(IND, fn))
        if len(df) < 240:
            continue
        df.index = pd.DatetimeIndex(df.index)
        b1, vol_ok, b1p_ok = signal_and_features(df)
        sets = {}
        for tag, mask in (("B1", b1), ("B1V", b1 & vol_ok), ("B1+", b1 & b1p_ok)):
            kept, last = [], -999
            for i in np.nonzero(mask.to_numpy())[0]:
                if i - last > 5:
                    kept.append(i)
                    last = i
            sets[tag] = kept
        for tag, idxs in sets.items():
            for i in idxs:
                d = df.index[i]
                if d < pd.Timestamp("2010-01-01"):
                    continue
                r = simulate(df, i)
                if r is None:
                    continue
                g = bool(gate.reindex([d], method="ffill").iloc[0]) if d <= ms.index[-1] else False
                rows.append({"tag": tag, "sym": fn[:-8], "date": d, "gate": g, **r})
    t = pd.DataFrame(rows)
    t["year"] = pd.to_datetime(t["date"]).dt.year
    if a.csv:
        t.to_csv(a.csv, index=False)
        print("明细 → %s" % a.csv)
    print("%-14s %7s %9s %9s %9s %9s %9s %6s" % ("组", "n", "净均值%", "胜率%", "中位%", "MFE%", "MAE%", "持有"))
    for lab, m in [("B1 全部", t.tag == "B1"), ("B1V 全部", t.tag == "B1V"),
                   ("B1V 门内", (t.tag == "B1V") & t.gate), ("B1V 门外", (t.tag == "B1V") & ~t.gate),
                   ("B1+ 全部", t.tag == "B1+"), ("B1+ 门内", (t.tag == "B1+") & t.gate), ("B1+ 门外", (t.tag == "B1+") & ~t.gate)]:
        x = t[m]
        if x.empty:
            continue
        print("%-14s %7d %9.2f %9.1f %9.2f %9.2f %9.2f %6.0f" % (
            lab, len(x), 100 * x.ret_net.mean(), 100 * (x.ret_net > 0).mean(),
            100 * x.ret_net.median(), 100 * x.mfe.mean(), 100 * x.mae.mean(), x.hold.median()))
    print("\n离场原因(B1+ 门内):")
    print(t[(t.tag == "B1+") & t.gate].why.value_counts().to_string())
    print("\nB1+ 门内 分年(净均值%/胜率%/n):")
    x = t[(t.tag == "B1+") & t.gate]
    g = x.groupby("year").ret_net.agg(["mean", lambda s: 100 * (s > 0).mean(), "size"])
    print(g.to_string(float_format=lambda v: "%.2f" % v))


if __name__ == "__main__":
    main()
