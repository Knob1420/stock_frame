#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_indicators.py —— 全市场指标计算 + parquet 落盘。
纯函数(macd/rsi/kdj,可单测) + run()(qlib 批量取数、按 indicators_conf 驱动、落 indicators 目录)。
每天由 update_data.py 全量重算;也可单独跑(经分批脚本,勿单进程全市场——OOM):
  /home/admin/stock_selection/.venv/bin/python build_indicators.py     # 全市场
  ... build_indicators.py --codes 000651,600941 --outdir tests/out     # 小样验证
"""
import argparse
import os

import numpy as np
import pandas as pd

from indicators_conf import INDICATORS

QLIB_URI = "/home/admin/stockdata/qlib_bin"
OUT_DIR = "/home/admin/stockdata/indicators"   # 数据与代码分离:parquet 落仓库外(2026-09-18)


# ---------- 纯函数(单测见 tests/test_indicators.py) ----------
def macd(close: pd.Series, fast=12, slow=26, signal=9) -> pd.DataFrame:
    """国内口径:DIF=EMA12-EMA26, DEA=EMA9(DIF), 柱=2*(DIF-DEA)。"""
    dif = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd_dif": dif, "macd_dea": dea, "macd_hist": 2 * (dif - dea)})


def rsi(close: pd.Series, period=14) -> pd.DataFrame:
    """Wilder 平滑(ewm alpha=1/period)。首日 diff=NaN → RSI=NaN。"""
    ch = close.diff()
    up = ch.clip(lower=0)
    dn = (-ch).clip(lower=0)
    au = up.ewm(alpha=1 / period, adjust=False).mean()
    ad = dn.ewm(alpha=1 / period, adjust=False).mean()
    return pd.DataFrame({"rsi_%d" % period: 100 * au / (au + ad)})


def kdj(high: pd.Series, low: pd.Series, close: pd.Series, n=9, k_p=3, d_p=3) -> pd.DataFrame:
    """国内软件口径:RSV=(C-LLV9)/(HHV9-LLV9)*100; K=SMA(RSV,3,1) 即 ewm alpha=1/3; J=3K-2D。
    HHV==LLV(一字/无波动)时 RSV 记 NaN。"""
    llv = low.rolling(n, min_periods=1).min()
    hhv = high.rolling(n, min_periods=1).max()
    rng = (hhv - llv).replace(0, np.nan)
    rsv = (close - llv) / rng * 100
    k = rsv.ewm(alpha=1 / k_p, adjust=False).mean()
    d = k.ewm(alpha=1 / d_p, adjust=False).mean()
    return pd.DataFrame({"kdj_k": k, "kdj_d": d, "kdj_j": 3 * k - 2 * d})


def ma(close: pd.Series, windows) -> pd.DataFrame:
    """简单算术均线族,min_periods=n(不足 NaN,与通达信 MA 一致)。"""
    return pd.DataFrame({"ma%d" % n: close.rolling(n, min_periods=n).mean() for n in windows})


def vma(volume: pd.Series, windows) -> pd.DataFrame:
    """量能均线族,口径同 ma。"""
    return pd.DataFrame({"vma%d" % n: volume.rolling(n, min_periods=n).mean() for n in windows})


def boll(close: pd.Series, n=20, p=2.0) -> pd.DataFrame:
    """BOLL:MID=MA(C,N);UPPER/LOWER=MID±p×STD(C,N)。STD 为样本标准差(ddof=1,通达信口径)。"""
    mid = close.rolling(n, min_periods=n).mean()
    sd = close.rolling(n, min_periods=n).std(ddof=1)
    return pd.DataFrame({"boll_upper": mid + p * sd, "boll_mid": mid, "boll_lower": mid - p * sd})


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n=14) -> pd.DataFrame:
    """ATR=MA(TR,N) 国内口径(非 Wilder 平滑)。TR=MAX(H-L,|H-昨收|,|L-昨收|);首 bar 无昨收→H-L。"""
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return pd.DataFrame({"atr%d" % n: tr.rolling(n, min_periods=n).mean()})


# FUNCS:注册表驱动 —— 名称 -> (函数, 输入字段)
FUNCS = {
    "macd": lambda g: macd(g["$close"], **INDICATORS["macd"]["params"]),
    "rsi":  lambda g: rsi(g["$close"], **INDICATORS["rsi"]["params"]),
    "kdj":  lambda g: kdj(g["$high"], g["$low"], g["$close"], **INDICATORS["kdj"]["params"]),
    "ma":   lambda g: ma(g["$close"], **INDICATORS["ma"]["params"]),
    "vma":  lambda g: vma(g["$volume"], **INDICATORS["vma"]["params"]),
    "boll": lambda g: boll(g["$close"], **INDICATORS["boll"]["params"]),
    "atr":  lambda g: atr(g["$high"], g["$low"], g["$close"], **INDICATORS["atr"]["params"]),
    "raw":  lambda g: g[["$open", "$high", "$low", "$close", "$volume", "$factor", "$amount"]]
                   .rename(columns=lambda c: c.lstrip("$")),
}


# ---------- 批量取数 + 落盘 ----------
def run(codes=None, outdir=None, raw_symbols=False):
    import qlib
    from qlib.data import D
    qlib.init(provider_uri=QLIB_URI, region="cn")
    out_dir = outdir or OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    if raw_symbols:
        syms = codes
    elif codes:
        syms = [("sh" if c[0] == "6" else "bj" if c[0] in "48" or c.startswith("92") else "sz") + c for c in codes]
    else:                                        # 全市场:读 instruments/all.txt 首列
        syms = [ln.split("\t")[0].lower()
                for ln in open(os.path.join(QLIB_URI, "instruments", "all.txt"), encoding="utf-8").read().splitlines() if ln.strip()]
    df = D.features(syms, ["$open", "$high", "$low", "$close", "$volume", "$factor", "$amount"],
                    disk_cache=0)
    ok = skip = 0
    for sym, g in df.groupby(level=0):
        try:
            g = g.droplevel(0).dropna(subset=["$close"])
            if len(g) < 30:
                skip += 1
                continue
            parts = [FUNCS[name](g) for name in INDICATORS]        # 注册表驱动
            out = pd.concat(parts, axis=1)
            out.index = [str(x)[:10] for x in g.index]
            out.to_parquet(os.path.join(out_dir, "%s.parquet" % str(sym).lower()))
            ok += 1
        except Exception as e:                                     # 单只失败不阻断
            skip += 1
            print("  ⚠ %s 失败: %s" % (sym, e))
    print("指标完成: 成功 %d / 跳过 %d / 输出 %s" % (ok, skip, out_dir))
    return ok, skip


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None, help="逗号分隔纯代码,如 000651,600941;缺省=全市场")
    ap.add_argument("--symbols", default=None, help="逗号分隔完整符号,如 sh600941,sz000651;跳过前缀转换")
    ap.add_argument("--outdir", default=None)
    a = ap.parse_args()
    syms = a.symbols.split(",") if a.symbols else (a.codes.split(",") if a.codes else None)
    run(codes=syms, outdir=a.outdir, raw_symbols=bool(a.symbols))
