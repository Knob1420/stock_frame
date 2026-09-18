# -*- coding: utf-8 -*-
"""research 测试公共:合成 research 用 parquet(26列子集,含 ma20/vma20/amount)。"""
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def make_rdf(n=600, seed=7, base=10.0, drift=0.0):
    """合成 research 输入:assemble 所需 6 列 + ma20/vma20/amount/ma240。drift=每根bar对数漂移。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-09-17", periods=n).strftime("%Y-%m-%d")
    close = base * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    high = close * (1 + rng.uniform(0, 0.02, n))
    low = close * (1 - rng.uniform(0, 0.02, n))
    vol = pd.Series(rng.uniform(8e6, 3e7, n), index=idx)
    df = pd.DataFrame({"open": close, "high": high, "low": low, "close": close,
                       "volume": vol, "kdj_j": rng.uniform(0, 100, n),
                       "ma20": close * 0.98, "ma240": base * 0.9, "vma20": vol,
                       "amount": (close * vol) / 1000.0},      # 千元口径(元/1000)
                      index=idx)
    return df


def write_pq(path, df):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_parquet(path)
