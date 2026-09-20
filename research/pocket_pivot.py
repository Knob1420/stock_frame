# -*- coding: utf-8 -*-
"""pocket_pivot.py —— 口袋支点公式回测(用户 2026-09-20 通达信公式翻译)。
SL −7% / TP +20% / 10 日到期;T+1 开盘入场,开盘≥昨收×1.097 无法成交;
每信号输出 [收益, MFE, MAE, 持有天数, 离场原因]。
口径披露:RPS=全市场横截面百分位(当日全 A 排名,等价 EXTDATA_USER);
KD8(换手≤15%,需流通股本 FINANCE(7))本地无数据,跳过;
BARSSINCEN(XG,20)=0 → 20 日冷却去重;AMO 用 amount 列(相对比较,单位无关)。
用法: python pocket_pivot.py [--csv out/pocket_trades.csv]
"""
import argparse
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
IND = os.path.join(os.path.dirname(HERE), "stockdata", "indicators")
OUT = os.path.join(HERE, "out")

SL, TP, HOLD, COST = -0.07, 0.20, 10, 0.0035


def build_kd1() -> set:
    """pass1:全市场 RPS 横截面 → KD1 = RPS250>=87 OR RPS120>=90 OR RPS50>=90。"""
    rows = {"date": [], "sym": [], "r50": [], "r120": [], "r250": []}
    for fn in sorted(os.listdir(IND)):
        if not fn.endswith(".parquet") or fn.startswith(("sh00", "sz39")):
            continue
        c = pd.read_parquet(os.path.join(IND, fn), columns=["close"])["close"]
        if len(c) < 300:
            continue
        c.index = pd.DatetimeIndex(c.index)
        r50 = (c / c.shift(50)).iloc[250:]
        r120 = (c / c.shift(120)).iloc[250:]
        r250 = (c / c.shift(250)).iloc[250:]
        rows["date"].extend(r50.index)
        rows["sym"].extend([fn[:-8]] * len(r50))
        rows["r50"].extend(r50.values)
        rows["r120"].extend(r120.reindex(r50.index).values)
        rows["r250"].extend(r250.values)
    d = pd.DataFrame(rows)
    g = d.groupby("date")[["r50", "r120", "r250"]]
    d[["r50", "r120", "r250"]] = g.rank(pct=True) * 100
    kd1 = d[(d.r250 >= 87) | (d.r120 >= 90) | (d.r50 >= 90)]
    s = set(zip(kd1.sym, kd1.date))
    print("KD1 通过(日期,股票)对:%d" % len(s))
    return s


def xg_signal(df: pd.DataFrame, sig_lim: float = 1.099) -> pd.Series:
    """KD2..KD9(除 KD8)+ XG;20 日冷却。"""
    c, o, h, l = (df[x] for x in ("close", "open", "high", "low"))
    amo = df["amount"]
    pct = c / c.shift(1)

    fk21 = amo == amo.rolling(10).max()
    fk22 = pct > sig_lim   # 分板块:主板1.099/创科1.199/北交1.299
    fk23 = amo / amo.rolling(10).mean() > 2
    kd2 = fk21 | fk22 | fk23

    ma = {n: c.rolling(n).mean() for n in (10, 50, 90, 100, 120)}
    hh90, hh100, hh250 = h.rolling(90).max(), h.rolling(100).max(), h.rolling(250).max()
    fk311 = (c > ma[90]) & (ma[90] >= ma[90].shift(5)) & (h >= hh90)
    fk321 = (c > ma[100]) & (ma[100] >= ma[100].shift(5)) & (h >= hh100) & (ma[90] >= ma[90].shift(5))
    fk331 = (c > ma[120]) & (ma[120] >= ma[120].shift(2))
    kd3 = fk311 | fk321 | fk331

    fk250 = h >= hh250
    kd4 = (l.rolling(15).min() > l.rolling(50).min() * 0.995) | fk250

    # 动态窗口:120 日最高点以来的最低价(numpy 逐 bar,~1μs/bar)
    hv, lv = h.values, l.values
    n = len(df)
    t1 = np.zeros(n, dtype=int)
    l120 = np.full(n, np.nan)
    for i in range(119, n):
        w = hv[i - 119: i + 1]
        b = i - 119 + int(np.argmax(w))
        t1[i] = i - b
        l120[i] = lv[b:i + 1].min()
    h120s = pd.Series(hv, index=df.index).rolling(120).max()
    l120s = pd.Series(l120, index=df.index)
    fk51 = l.rolling(40).min() / h.rolling(120).max() > 0.5
    kd5 = (l120s / h120s > 0.54) & (fk51 | fk250)

    ll15, ll50, ll100 = l.rolling(15).min(), l.rolling(50).min(), l.rolling(100).min()
    fk41 = fk250 | (ll15 > ll50)
    fk42 = (ll15 == ll50) & (ll15 > ll100) & (h / hh250 > 0.88)
    fk43 = (ll15 == ll50) & (ll15 > ll100) & (h / hh250 > 0.75) & (h >= h.rolling(40).max()) & (pct > 1.07)
    kd6 = fk41 | fk42 | fk43

    kd7 = pct >= 1.05
    kd9 = (l.shift(1) <= ma[50].shift(1) * 1.24) | (l.shift(1) <= ma[10].shift(1) * 1.03)

    xg = kd2 & kd3 & kd4 & kd5 & kd6 & kd7 & kd9
    xg = xg.fillna(False)
    # BARSSINCEN(XG,20)=0:20 日内首次
    out = xg.copy()
    last = -999
    xv = xg.values
    for i in np.nonzero(xv)[0]:
        if i - last > 20:
            out.iloc[i] = True
            last = i
        else:
            out.iloc[i] = False
    return out


def simulate(df, i, buy_lim=0.097, slip=0.001):
    """T+1 开盘入场;最早 T+2 卖;跳空穿价按开盘成交,盘中触价按触价;HOLD 日到期收盘。"""
    if i + 1 >= len(df):
        return None
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    entry = o[i + 1]
    if np.isnan(entry) or entry >= c[i] * (1 + buy_lim):
        return None
    cost = 0.00025 * 2 + 0.0005 + slip * 2          # 佣金双边+印花卖+滑点双边
    mfe, mae = 0.0, 0.0
    for k in range(i + 1, min(i + 1 + HOLD, len(df))):
        mfe = max(mfe, h[k] / entry - 1)
        mae = min(mae, l[k] / entry - 1)
        if k <= i + 1:                               # T+1 买入日不可卖
            continue
        if o[k] / entry - 1 <= SL:
            gross, why = o[k] / entry - 1, "跳空止损"
            break
        if l[k] / entry - 1 <= SL:
            gross, why = SL, "止损"
            break
        if o[k] / entry - 1 >= TP:
            gross, why = o[k] / entry - 1, "跳空止盈"
            break
        if h[k] / entry - 1 >= TP:
            gross, why = TP, "止盈"
            break
    else:
        k = min(i + HOLD, len(df) - 1)
        gross, why = c[k] / entry - 1, "到期"
    ke = min(i + 1 + HOLD - 1, len(df) - 1)
    fwd_exp = c[ke] / entry - 1.0
    return {"ret_gross": gross, "ret_net": (1 + gross) * (1 - cost) - 1,
            "fwd_exp": fwd_exp, "fwd_exp_net": (1 + fwd_exp) * (1 - cost) - 1,
            "mfe": mfe, "mae": mae, "hold": k - i, "why": why}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None)
    a = ap.parse_args(argv)
    kd1 = build_kd1()
    rows = []
    for fn in sorted(os.listdir(IND)):
        if not fn.endswith(".parquet") or fn.startswith(("sh00", "sz39")):
            continue
        df = pd.read_parquet(os.path.join(IND, fn))
        if len(df) < 300:
            continue
        df.index = pd.DatetimeIndex(df.index)
        b_ = fn[:2] if not fn.startswith("bj") else "bj"
        sig_lim = {"sh6": 1.099}.get(b_, 1.099)
        if fn.startswith(("sz30", "sh68")):
            sig_lim, buy_lim, slip = 1.199, 0.197, 0.002
        elif fn.startswith("bj"):
            sig_lim, buy_lim, slip = 1.299, 0.297, 0.003
        else:
            buy_lim, slip = 0.097, 0.001
        sig = xg_signal(df, sig_lim)
        for i in np.nonzero(sig.values)[0]:
            d = df.index[i]
            if d < pd.Timestamp("2010-01-01"):
                continue
            if (fn[:-8], d) not in kd1:
                continue
            r = simulate(df, i, buy_lim, slip)
            if r:
                rows.append({"sym": fn[:-8], "date": d, **r})
    t = pd.DataFrame(rows)
    t["year"] = pd.to_datetime(t["date"]).dt.year
    if a.csv:
        t.to_csv(a.csv, index=False)
        print("明细 → %s" % a.csv)
    if t.empty:
        print("无信号")
        return
    print("\n信号 n=%d" % len(t))
    print("[TP20/SL7 规则]   净均值 %+.2f%% 胜率 %.1f%% 中位 %+.2f%% | MFE %+.2f%% MAE %+.2f%% 持有中位 %.0f 日" % (
        100 * t.ret_net.mean(), 100 * (t.ret_net > 0).mean(),
        100 * t.ret_net.median(), 100 * t.mfe.mean(), 100 * t.mae.mean(), t.hold.median()))
    print("[纯持有10日收盘]  净均值 %+.2f%% 胜率 %.1f%% 中位 %+.2f%%" % (
        100 * t.fwd_exp_net.mean(), 100 * (t.fwd_exp_net > 0).mean(), 100 * t.fwd_exp_net.median()))
    print("\n离场原因:")
    print(t.why.value_counts().to_string())
    print("\n分年(净均值%/胜率%/n):")
    g = t.groupby("year").ret_net.agg(["mean", lambda s: 100 * (s > 0).mean(), "size"])
    print(g.to_string(float_format=lambda v: "%.2f" % v))


if __name__ == "__main__":
    main()
