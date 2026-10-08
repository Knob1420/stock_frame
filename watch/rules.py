# -*- coding: utf-8 -*-
"""盘后监控规则注册表。

规则只回答“今天条件是否成立”，状态转换、组合评分、冷却和推送均由
``scan.py`` 负责。``RULE_META`` 给出规则的默认展示名和提醒权重；YAML 可以
逐条覆盖。这样新增规则时，判断逻辑和通知策略不会混在一起。
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
RULE_META = {}


def register(name, *, label, category, weight, standalone=False, kind="watch"):
    def deco(fn):
        RULES[name] = fn
        RULE_META[name] = {
            "label": label,
            "category": category,
            "weight": int(weight),
            "standalone": bool(standalone),
            "kind": kind,
        }
        return fn
    return deco


def _with_j(df: pd.DataFrame) -> pd.DataFrame:
    if "j" not in df.columns:
        df = df.assign(j=df["kdj_j"])
    return df


def _b1_core(df, no_good=False):
    """B1 条件；no_good=True 仅为兼容旧配置，此时只检查 J≤15。"""
    if no_good:
        return df["j"].le(15).fillna(False)
    return baseline_original(df).fillna(False)


@register("b1", label="日线B1进入观察", category="daily_setup", weight=3)
def rule_b1(df, params):
    """日线 B1 条件。默认使用原公式；``no_good=true`` 才退化为仅 J≤15。"""
    sig = _b1_core(_with_j(df), bool(params.get("no_good", False)))
    return bool(sig.values[-1])          # 去重交给引擎的状态转换(False→True 才报)


@register("b1_hook", label="B1低位首次拐头", category="confirmation", weight=5,
          standalone=True)
def rule_b1_hook(df, params):
    """J 低位后的首次向上拐头；这是观察确认信号，不等同于已验证买点。"""
    from kdj.layers import ema2, long_ma

    d = _with_j(df)
    threshold = float(params.get("threshold", 15.0))
    good = ema2(d["close"]) > long_ma(d["close"])
    pct = d["close"] / d["close"].shift(1) - 1.0
    amplitude = (d["high"] - d["low"]) / d["close"].shift(1)
    calm_bar = (
        pct.abs().le(float(params.get("max_abs_pct", 0.03)))
        & amplitude.le(float(params.get("max_amplitude", 0.07)))
    )
    up = (d["j"] > d["j"].shift(1)).fillna(False)
    low_positions = np.flatnonzero(d["j"].le(threshold).to_numpy())
    if not len(low_positions):
        return False
    last_low = int(low_positions[-1])
    hook = bool(up.iloc[-1] and int(up.iloc[last_low:].sum()) == 1)
    if not bool(params.get("require_good", True)):
        good = pd.Series(True, index=d.index)
    return bool(good.fillna(False).iloc[-1] and calm_bar.fillna(False).iloc[-1] and hook)


@register("weekly_b1", label="周线B1进入观察", category="weekly_setup", weight=2)
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


@register("near_ma", label="回到均线支撑区", category="support", weight=1)
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


@register("ma_reclaim", label="收盘重新站回均线", category="confirmation", weight=4)
def rule_ma_reclaim(df, params):
    """前一日在线下、当日收盘重新站上均线，且离均线不过远。"""
    n = int(params.get("period", 20))
    tolerance = float(params.get("tolerance", 0.02))
    ma = df["close"].rolling(n, min_periods=n).mean()
    if len(df) < n + 1 or pd.isna(ma.iloc[-1]) or pd.isna(ma.iloc[-2]):
        return False
    previous_below = df["close"].iloc[-2] <= ma.iloc[-2]
    current_above = ma.iloc[-1] < df["close"].iloc[-1] <= ma.iloc[-1] * (1 + tolerance)
    return bool(previous_below and current_above)


@register("box_touch_lower", label="箱体下沿得到承接", category="support", weight=5,
          standalone=True)
def rule_box_lower(df, params):
    """箱体下沿:当日最低现价触及 lower×1.01 内。箱体按行情软件可见价(不复权)配置,
    内部用 factor 列换算(现价 = 后复权价/factor)。"""
    lower = float(params["lower"])
    f = df["factor"].iloc[-1]
    low_now = df["low"].iloc[-1] / f                          # 当日现价口径
    close_now = df["close"].iloc[-1] / f
    return bool(low_now <= lower * 1.01 and close_now >= lower * 0.97)   # 贴沿可报;跌破过深(逻辑失效)不报


@register("box_breakdown", label="收盘跌破箱体下沿", category="risk", weight=6,
          standalone=True, kind="risk")
def rule_box_breakdown(df, params):
    """收盘首次跌破人工箱体下沿；用于风险提醒。"""
    if len(df) < 2:
        return False
    lower = float(params["lower"])
    now = df["close"].iloc[-1] / df["factor"].iloc[-1]
    previous = df["close"].iloc[-2] / df["factor"].iloc[-2]
    return bool(previous >= lower and now < lower)


@register("breakout", label="放量突破前高", category="breakout", weight=5,
          standalone=True)
def rule_breakout(df, params):
    """收盘突破此前 N 日高点，并达到指定量比。"""
    n = int(params.get("period", 20))
    min_volr = float(params.get("min_volr", 1.5))
    if len(df) < max(n + 1, 21):
        return False
    prior_high = df["high"].shift(1).rolling(n, min_periods=n).max().iloc[-1]
    vma20 = df["volume"].rolling(20, min_periods=20).mean().iloc[-1]
    return bool(df["close"].iloc[-1] > prior_high and
                pd.notna(vma20) and df["volume"].iloc[-1] / vma20 >= min_volr)


@register("volume_dry_up", label="回调缩量", category="volume", weight=1)
def rule_dry_up(df, params):
    """缩量。设置 ``ratio`` 时按 V/VMA20，否则兼容旧的 250 日分位口径。"""
    v = df["volume"]
    if "ratio" in params:
        vma20 = v.rolling(20, min_periods=20).mean().iloc[-1]
        return bool(pd.notna(vma20) and v.iloc[-1] / vma20 <= float(params["ratio"]))
    q = v.iloc[-1] < v.iloc[-250:].quantile(float(params.get("pct", 0.2)))
    return bool(q)
