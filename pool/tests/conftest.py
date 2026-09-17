# -*- coding: utf-8 -*-
"""pool 测试公共:仓库根入 sys.path(kdj 包可导入)+ 合成 parquet 构造器。"""
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def make_df(n=600, seed=7, base=10.0):
    """合成 26 列子集 DataFrame(升序 bdate 索引),够 scan/lookup 用。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-02", periods=n).strftime("%Y-%m-%d")
    close = pd.Series(base + np.cumsum(rng.normal(0, 0.03, n)), index=idx)
    high = close * (1 + rng.uniform(0, 0.02, n))
    low = close * (1 - rng.uniform(0, 0.02, n))
    vol = pd.Series(rng.uniform(2e5, 2e6, n), index=idx)
    j = pd.Series(rng.uniform(0, 100, n), index=idx)
    # amount=0.1×close×volume(千元)与真实数据恒等式一致:复权价×复权量=真实成交额/1000
    df = pd.DataFrame({"open": close, "high": high, "low": low, "close": close,
                       "volume": vol, "factor": 1.0, "amount": 0.1 * close * vol,
                       "kdj_j": j, "ma240": base * 1.0, "boll_mid": base, "atr14": 0.5,
                       "macd_hist": 0.0})
    return df


def write_pq(path, df):
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_parquet(path)
