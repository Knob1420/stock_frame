# -*- coding: utf-8 -*-
"""layers.py —— 三层条件纯函数(DataFrame→布尔列)。列:open/high/low/close/volume/j。
口径与通达信对齐:MA 族 min_periods=n(不足为 NaN→False);LLV/HHV 族 min_periods=1。
spec: 选股/docs/superpowers/specs/2026-09-08-kdj-rebound-design.md §4"""
import numpy as np
import pandas as pd


def ema2(close, n=10):
    """SHORT := EMA(EMA(C,10),10)。"""
    e = close.ewm(span=n, adjust=False).mean()
    return e.ewm(span=n, adjust=False).mean()


def long_ma(close):
    """LONG := (MA14+MA28+MA57+MA114)/4。不足 114 根为 NaN(剔次新)。"""
    ms = [close.rolling(n, min_periods=n).mean() for n in (14, 28, 57, 114)]
    return sum(ms) / 4.0


def cross_up(a, b):
    """CROSS(a,b):今 a>b 且昨 a<=b。首 bar 昨值 NaN → False。"""
    return (a > b) & (a.shift(1) <= b.shift(1))


def bars_since(cond):
    """BARSLAST(cond):距最近一次 True 的 bar 数(今天 True→0);从未 True→NaN。"""
    pos = pd.Series(np.arange(len(cond)), index=cond.index)
    return pos - pos.where(cond).ffill()


def l1_good(df):
    s, l = ema2(df["close"]), long_ma(df["close"])
    return s > l


def l1_stable(df, n=5):
    """+ LONG>=REF(LONG,5):长期锚不下行。"""
    s, l = ema2(df["close"]), long_ma(df["close"])
    return (s > l) & (l >= l.shift(n))


def l1_age(df, lo=5, hi=80):
    """+ BARSLAST(CROSS(SHORT,LONG)) ∈ [lo,hi]。"""
    a = bars_since(cross_up(ema2(df["close"]), long_ma(df["close"])))
    return (a >= lo) & (a <= hi)


def l2_low(j, w=5, thr=15.0):
    """LLV(J,W)<=15:近 W 根(含今)J 曾深度低位。"""
    return j.rolling(w, min_periods=1).min() <= thr


def l2_shrink(volume, w=5, ratio=0.8, fast=3, slow=20):
    """LLV(MA(V,3),W) < ratio×MA(V,20):回调段曾明显缩量。"""
    maf_min = (volume.rolling(fast, min_periods=fast).mean()
               .rolling(w, min_periods=1).min())
    mas = volume.rolling(slow, min_periods=slow).mean()
    return maf_min < ratio * mas


def l2_depth(close, high, n=60, floor=-0.25):
    """C/HHV(H,60)-1 > floor:回撤有界。HHV 用可得数据(min_periods=1,通达信 LLV/HHV 口径)。"""
    return (close / high.rolling(n, min_periods=1).max() - 1.0) > floor


def l2_ma240(close):
    """可选对比项:C>MA240(年线上方)。"""
    return close > close.rolling(240, min_periods=240).mean()


def l3_hook(j):
    """J>REF(J,1)。"""
    return j > j.shift(1)


def count_since(event, anchor):
    """COUNT(event, BARSLAST(anchor)+1):自最近一次 anchor=True(含该日)到今天的 event 次数。"""
    ce = event.cumsum()
    base_ce = ce.where(anchor).ffill()      # 最近锚日的累计
    base_ev = event.where(anchor).ffill()   # 锚日当日 event(0/1)
    return ce - base_ce + base_ev


def l3_hook_first(j, thr=15.0):
    """首次拐头:J>REF(J,1) AND COUNT(J>REF(J,1), BARSLAST(J<=15)+1)=1(通达信逐字对齐)。"""
    up = j > j.shift(1)
    low = j <= thr
    return up & (count_since(up, low) == 1)


def l3_confirm_bull(df):
    return df["close"] > df["open"]


def l3_confirm_ma5(df):
    return df["close"] > df["close"].rolling(5, min_periods=5).mean()


def l3_confirm_volume(df, n=20):
    v20 = df["volume"].rolling(n, min_periods=n).mean()
    return (df["volume"] > v20) & (df["close"] > df["close"].shift(1))


def l3_pct(df, lim=0.03):
    """对比项 d:原公式 |PCT|<=3% 复刻。"""
    return ((df["close"] / df["close"].shift(1)) - 1.0).abs() <= lim


DEFAULT_CFG = {
    "l1": ["good", "stable", "age"], "age_lo": 5, "age_hi": 80,
    "l2": ["low", "shrink", "depth"], "w": 5, "shrink": 0.8, "depth": -0.25,
    "ma240": False,
    "l3": ["hook"], "pct": 0.03,
}


def signal(df, cfg):
    """三层组装。cfg 为 DEFAULT_CFG 的覆写(dict 合并);NaN 一律按 False。
    消融:去掉列表元素 / 换 l3 元素 / 改参数。"""
    c = {**DEFAULT_CFG, **cfg}
    j = df["j"]
    conds = []
    if "good" in c["l1"]: conds.append(l1_good(df))
    if "stable" in c["l1"]: conds.append(l1_stable(df))
    if "age" in c["l1"]: conds.append(l1_age(df, lo=c["age_lo"], hi=c["age_hi"]))
    if c["ma240"]: conds.append(l2_ma240(df["close"]))
    if "low" in c["l2"]: conds.append(l2_low(j, w=c["w"]))
    if "shrink" in c["l2"]:
        conds.append(l2_shrink(df["volume"], w=c["w"], ratio=c["shrink"]))
    if "depth" in c["l2"]:
        conds.append(l2_depth(df["close"], df["high"], floor=c["depth"]))
    if "first" in c["l3"]: conds.append(l3_hook_first(j))
    else: conds.append(l3_hook(j))
    if "bull" in c["l3"]: conds.append(l3_confirm_bull(df))
    if "ma5" in c["l3"]: conds.append(l3_confirm_ma5(df))
    if "vol" in c["l3"]: conds.append(l3_confirm_volume(df))
    if "pct" in c["l3"]: conds.append(l3_pct(df, lim=c["pct"]))
    out = pd.Series(True, index=df.index)
    for cond in conds:
        out &= cond.fillna(False)
    return out


def baseline_original(df):
    """原公式复刻(spec §1):GOOD AND J<=15 AND ABS(PCT)<=3% AND AMP<=7%。"""
    good = ema2(df["close"]) > long_ma(df["close"])
    pct = df["close"] / df["close"].shift(1) - 1.0
    amp = (df["high"] - df["low"]) / df["close"].shift(1)
    out = good & (df["j"] <= 15) & pct.abs().le(0.03) & amp.le(0.07)
    return out.fillna(False)
