# -*- coding: utf-8 -*-
"""build_market_state.py —— 市场环境日表(B1+ 环境层数据基础)。
逐日(全市场股票横截面,剔指数)计算:
  adv_ratio      涨跌家数宽度(收>昨收 占比)
  above_ma20/60  站上 20/60 日线占比(趋势宽度)
  amt_ratio      全市场成交额 / 其 20 日均(量能状态)
  eq_ret20       全市场等权 20 日收益(小盘代理)
指数侧(沪深300):
  idx_ret20, idx_dist240(距MA240), idx_slope20(MA20 五日斜率), idx_vol20(20日已实现波动)
rotation = eq_ret20 − idx_ret20(小大盘相对强弱)
用法: python build_market_state.py   # → out/market_state.parquet
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from build_b1_events import IND_DIR, OUT_DIR, INDEX_PREFIX, MIN_BARS  # noqa: E402

ACC = None   # dict[str, Series]:n/adv/ma20c/ma60c/amt/ret20 的逐日向量化累加


def build():
    global ACC
    syms = sorted(f[:-8] for f in os.listdir(IND_DIR)
                  if f.endswith(".parquet") and not f.startswith(INDEX_PREFIX))
    ACC = {k: pd.Series(0.0, index=pd.DatetimeIndex([]))
           for k in ("n", "adv", "ma20c", "ma60c", "amt", "ret20")}
    for k, sym in enumerate(syms, 1):
        try:
            df = pd.read_parquet(os.path.join(IND_DIR, "%s.parquet" % sym),
                                 columns=["close", "ma20", "ma60", "amount"])
        except Exception as e:
            print("%s 读取失败: %s" % (sym, e))
            continue
        if len(df) < MIN_BARS:
            continue
        df.index = pd.DatetimeIndex(df.index)
        c = df["close"]
        adv = (c > c.shift(1)).astype(float)
        a20 = (c > df["ma20"]).fillna(False).astype(float)
        a60 = (c > df["ma60"]).fillna(False).astype(float)
        r20 = (c / c.shift(20) - 1.0).fillna(0.0)
        amt = df["amount"].fillna(0.0)
        for key, s in (("n", adv * 0 + 1), ("adv", adv), ("ma20c", a20),
                       ("ma60c", a60), ("amt", amt), ("ret20", r20)):
            ACC[key] = ACC[key].add(s, fill_value=0.0)
        if k % 1000 == 0:
            print("[%d/%d] 日历 %d 日" % (k, len(syms), len(ACC["n"])))
    m = pd.DataFrame(ACC).sort_index()
    m.index.name = "date"
    m["adv_ratio"] = m["adv"] / m["n"]
    m["above_ma20"] = m["ma20c"] / m["n"]
    m["above_ma60"] = m["ma60c"] / m["n"]
    m["amt_ratio"] = m["amt"] / m["amt"].rolling(20, min_periods=20).mean()
    m["eq_ret20"] = m["ret20"] / m["n"]

    idx = pd.read_parquet(os.path.join(IND_DIR, "sh000300.parquet"))[["close"]]
    idx.index = pd.DatetimeIndex(idx.index)
    c = idx["close"]
    ma20 = c.rolling(20, min_periods=20).mean()
    ma240 = c.rolling(240, min_periods=240).mean()
    idx_state = pd.DataFrame({
        "idx_ret20": c / c.shift(20) - 1.0,
        "idx_dist240": c / ma240 - 1.0,
        "idx_slope20": ma20 / ma20.shift(5) - 1.0,
        "idx_vol20": (c / c.shift(1) - 1.0).rolling(20, min_periods=20).std(),
    })
    m = m.join(idx_state)
    m["rotation"] = m["eq_ret20"] - m["idx_ret20"]
    out = os.path.join(OUT_DIR, "market_state.parquet")
    m.to_parquet(out)
    print("→ %s(%d 日,2010 起 %d 日)" % (out, len(m), (m.index >= "2010-01-01").sum()))
    return m


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    build()
