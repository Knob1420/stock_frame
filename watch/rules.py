# -*- coding: utf-8 -*-
"""rules.py —— 盘后监控规则注册表(纯函数:df+params → 当日条件是否成立)。
约定:返回 bool(当日"条件成立"),引擎负责 False→True 转换才报警;
连续型信号(b1/weekly_b1)自带 5 bar 冷却防轰炸。
数据:stockdata/indicators parquet(日线后复权+指标列),零外部依赖。
新增规则:写函数 + @register 即可,scan.py 自动可用。
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from kdj.layers import baseline_original                    # noqa: E402  B1 原公式


def kdj_calc(high, low, close, n=9, k_p=3, d_p=3):
    """通达信口径 KDJ(与 stockdata/build_indicators.kdj 同实现,内联避免依赖链)。"""
    llv = low.rolling(n, min_periods=1).min()
    hhv = high.rolling(n, min_periods=1).max()
    rsv = (close - llv) / (hhv - llv).replace(0, np.nan) * 100
    k = rsv.ewm(alpha=1 / k_p, adjust=False).mean()
    d = k.ewm(alpha=1 / d_p, adjust=False).mean()
    return pd.DataFrame({"kdj_k": k, "kdj_d": d, "kdj_j": 3 * k - 2 * d})


RULES = {}


def register(name):
    def deco(fn):
        RULES[name] = fn
        return fn
    return deco


def _with_j(df: pd.DataFrame) -> pd.DataFrame:
    if "j" not in df.columns:
        df = df.assign(j=df["kdj_j"])
    return df


def _b1_core(df, no_good=False):
    """B1 条件;no_good=True 为盯盘口径:只看 J≤15(金叉/小阴小阳都不管),冷却防连发。"""
    from kdj.layers import ema2, long_ma
    out = df["j"] <= 15
    if not no_good:
        pct = df["close"] / df["close"].shift(1) - 1.0
        amp = (df["high"] - df["low"]) / df["close"].shift(1)
        out = out & pct.abs().le(0.03) & amp.le(0.07) & (ema2(df["close"]) > long_ma(df["close"]))
    return out.fillna(False)


@register("b1")
def rule_b1(df, params):
    """日线 B1 原公式(尾 bar 是否触发,含 5 日簇冷却)。"""
    sig = _b1_core(_with_j(df), bool(params.get("no_good", False)))
    return bool(sig.values[-1])          # 去重交给引擎的状态转换(False→True 才报)


@register("weekly_b1")
def rule_weekly_b1(df, params):
    """周线 B1:日线重采样→同口径 KDJ→同一 B1 函数(一份逻辑两处复用)。"""
    w = pd.DataFrame({
        "open": df["open"].resample("W-FRI").first(),
        "high": df["high"].resample("W-FRI").max(),
        "low": df["low"].resample("W-FRI").min(),
        "close": df["close"].resample("W-FRI").last(),
        "volume": df["volume"].resample("W-FRI").sum(),
    }).dropna()
    k = kdj_calc(w["high"], w["low"], w["close"])
    w = w.join(k)
    sig = _b1_core(_with_j(w), bool(params.get("no_good", False)))
    return bool(sig.values[-1])


@register("near_ma")
def rule_near_ma(df, params):
    """收盘距 MA{period} ±tolerance 内(direction:up=须在线上)。"""
    n, tol = int(params.get("period", 60)), float(params.get("tolerance", 0.02))
    ma = df["close"].rolling(n, min_periods=n).mean()
    c, m = df["close"].iloc[-1], ma.iloc[-1]
    if np.isnan(m):
        return False
    near = abs(c / m - 1) <= tol and c >= m      # 默认"线上方回踩贴线";跌破=跌破,不算附近
    if params.get("direction") == "down":         # 反弹遇阻场景:线下方贴近
        near = abs(c / m - 1) <= tol and c < m
    return bool(near)


@register("box_touch_lower")
def rule_box_lower(df, params):
    """箱体下沿:当日最低现价触及 lower×1.01 内。箱体按行情软件可见价(不复权)配置,
    内部用 factor 列换算(现价 = 后复权价/factor)。"""
    lower = float(params["lower"])
    f = df["factor"].iloc[-1]
    low_now = df["low"].iloc[-1] / f                          # 当日现价口径
    close_now = df["close"].iloc[-1] / f
    return bool(low_now <= lower * 1.01 and close_now >= lower * 0.97)   # 贴沿可报;跌破过深(逻辑失效)不报


@register("volume_dry_up")
def rule_dry_up(df, params):
    """地量:当日成交量 < 250 日分位 20%。"""
    v = df["volume"]
    q = v.iloc[-1] < v.iloc[-250:].quantile(float(params.get("pct", 0.2)))
    return bool(q)
