# -*- coding: utf-8 -*-
"""测试公用:合成 indicators parquet(现价口径可控)与 1x1 PNG。"""
import base64

import pandas as pd

PNG1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def write_px(path, px, hi_pct=1.02, lo_pct=0.98, start="2026-08-03"):
    """合成 indicators parquet:现价序列 px;factor 后半段翻倍(后复权价翻倍,现价不变),
    用于断言现价口径(close=px*factor,high/low 同放大)。返回 DatetimeIndex。"""
    n = len(px)
    idx = pd.bdate_range(start, periods=n)
    factor = [1.0] * (n // 2) + [2.0] * (n - n // 2)
    df = pd.DataFrame({
        "close": [p * f for p, f in zip(px, factor)],
        "high": [p * hi_pct * f for p, f in zip(px, factor)],
        "low": [p * lo_pct * f for p, f in zip(px, factor)],
        "factor": factor,
    }, index=idx)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return idx
