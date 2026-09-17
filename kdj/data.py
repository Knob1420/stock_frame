# -*- coding: utf-8 -*-
"""data.py —— qlib_bin 加载:主板白名单池(新/旧两变体)、指标附加(复用 build_indicators.kdj)、
沪深300 指数(环境标签)。spec §5。"""
import os
import sys
import pandas as pd

STOCKDATA = "E:/quant/stockdata"
QLIB_URI = os.path.join(STOCKDATA, "qlib_bin")
FIELDS = ("$open", "$high", "$low", "$close", "$volume")
_inited = False


def _ensure_qlib():
    global _inited
    if not _inited:
        import qlib
        qlib.init(provider_uri=QLIB_URI, region="cn")
        _inited = True


def _filter_pool(syms, variant="new"):
    """new=白名单 sh60*/sz00*(spec §5,修复 301/689 漏网);
    old=复刻原公式排除法(300*/688* 前缀排除,其余股票全留)——量化 bug 影响。"""
    if variant not in ("new", "old"):
        raise ValueError("variant must be 'new' or 'old', got %r" % variant)
    out = []
    for sym in syms:
        code = sym[2:]
        if variant == "new":
            if sym.startswith(("sh60", "sz00")):      # sh00* 是指数族,天然排除
                out.append(sym)
        else:
            if sym.startswith("sh00"):                # 指数族排除
                continue
            if code.startswith("300") or code.startswith("688"):
                continue                              # 原公式排除项(301/689 漏网)
            out.append(sym)
    return out


def pool_syms(variant="new"):
    path = os.path.join(QLIB_URI, "instruments", "all.txt")
    syms = [ln.split("\t")[0].strip().lower()
            for ln in open(path, encoding="utf-8").read().splitlines() if ln.strip()]
    return _filter_pool(syms, variant)


def attach_indicators(g):
    """列名剥 $,附加 j(国内口径 KDJ,与通达信一致——直接复用,不重造)。"""
    if STOCKDATA not in sys.path:
        sys.path.append(STOCKDATA)      # append+守卫:避免逐股调用时膨胀/劫持模块名
    from build_indicators import kdj
    g = g.copy()
    g.columns = [c.lstrip("$") for c in g.columns]
    g["j"] = kdj(g["high"], g["low"], g["close"])["kdj_j"]
    return g


def load_market(start="2010-01-01", variant="new"):
    """全池 OHLCV,返回 (groupby 对象, 市场日历 Index)。"""
    _ensure_qlib()
    from qlib.data import D
    df = D.features(pool_syms(variant), list(FIELDS), start_time=start, disk_cache=0)
    cal = load_index().index                           # 指数日历=市场日历
    return df.groupby(level=0), cal


def load_index(sym="sh000300", n=240):
    """沪深300 收盘与 bull 标签(close>MA240)。已实测 qlib_bin 含 features/sh000300。"""
    _ensure_qlib()
    from qlib.data import D
    s = D.features([sym], ["$close"], disk_cache=0).xs(sym, level=0)["$close"].dropna()
    ma = s.rolling(n, min_periods=n).mean()
    return pd.DataFrame({"close": s, "bull": (s > ma).fillna(False)})
