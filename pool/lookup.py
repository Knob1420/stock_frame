# -*- coding: utf-8 -*-
"""lookup.py —— 同型桶:特征提取、全史回放、18桶统计表生成与查表(spec §6 + 6c 裁决)。
判定口径=池自己的结案规则(hit+8%/miss−5%/flat 10日,T+1起算),与回测 E1/E3 无关。
6c: J×回撤各 3 内档(共18桶,单日有效9);回放跳过收盘涨停代理(≥9.7%,与筛选口径对称);
表文件名带桶方案版本 -b2,旧 8 桶表(无后缀)不再被加载。"""
import glob
import json
import os

import numpy as np
import pandas as pd

from pool import cfg as C
from pool.pool import CLOSE_CFG

J_THR = 15.0          # 桶外边界=cfg L2 的 J 阈值(l2_low 默认 15,硬编码并注释)
DD_FLOOR_KEY = "depth"
BUCKET_VER = "b2"     # 桶方案版本(J 3档×dd 3档):方案再变更时递增,旧表不静默沿用
LIM_UP = 0.097        # 收盘涨停代理(≥9.7%):次日追高不可操作,回放跳过(与筛选硬过滤口径对称)


def feature_series(df):
    """df 需列 j/high/close/volume。返回特征 Series(dict):j_low/dd/age/shrink。"""
    import kdj.layers as L
    v = df["volume"]
    return {
        "j_low": df["j"].rolling(5, min_periods=1).min(),
        "dd": df["close"] / df["high"].rolling(60, min_periods=1).max() - 1,
        "age": L.bars_since(L.cross_up(L.ema2(df["close"]), L.long_ma(df["close"]))),
        "shrink": (v.rolling(3, min_periods=3).mean().rolling(5, min_periods=1).min()
                   / v.rolling(20, min_periods=20).mean()),
    }


def signal_features(df, i=-1):
    fs = feature_series(df)
    out = {}
    for k, s in fs.items():
        v = s.iloc[i]
        out[k] = float(v) if v == v else None                            # NaN→None
    return out


def bucket_key(env_bull, j_low, dd):
    """18桶键(J 深度 3 档 × 回撤 3 档 × 牛熊;单日有效 9 桶)。边界值归更深档。
    内部分档 5/10 与 -10%/-15% 为固定常数;外边界仍跟 cfg(J_THR=15、depth=-0.25),
    特征超界钳制到边缘桶(防换cfg后漏桶)。元组 arity 恒 3,scan 拼桶名自动适配。"""
    from kdj.layers import DEFAULT_CFG
    je = min(j_low, J_THR)
    de = min(max(dd, DEFAULT_CFG[DD_FLOOR_KEY]), 0.0)
    return ("bull" if env_bull else "bear",
            "j<=5" if je <= 5 else ("5<j<=10" if je <= 10 else "10<j<=15"),
            "dd>-10" if de > -0.10 else ("-15<dd<=-10" if de > -0.15 else "dd<=-15"))


def replay_outcome(df, i, cfg=CLOSE_CFG):
    """行 i 信号日的池口径结局。基准=信号日 close;窗口=i+1..i+10 行。数据不足→None。
    触发判定与 pool._trigger 同口径:严格越过阈值(±1e-9 容差),恰在阈值上不触发。
    信号日收盘涨幅 ≥9.7%(涨停代理,后复权 close/close[-1] 口径同筛选)→ None 跳过,
    统计与筛选口径对称(6c);首行无昨收,不判涨停。"""
    eps = 1e-9
    if i > 0 and df["close"].iloc[i] / df["close"].iloc[i - 1] - 1 >= LIM_UP:
        return None                                  # 收盘涨停:次日追高不可操作,不入统计
    base = df["close"].iloc[i]
    hu = (df["high"].iloc[i + 1: i + 1 + cfg["window"]] / base - 1).reset_index(drop=True)
    ld = (df["low"].iloc[i + 1: i + 1 + cfg["window"]] / base - 1).reset_index(drop=True)
    if len(hu) < cfg["window"]:
        return None
    hpos = int(np.argmax(hu.ge(cfg["hit"] + eps).values)) if hu.ge(cfg["hit"] + eps).any() else None
    mpos = int(np.argmax(ld.le(cfg["miss"] - eps).values)) if ld.le(cfg["miss"] - eps).any() else None
    if hpos is not None and mpos is not None:
        t, pos = ("miss", mpos) if mpos <= hpos else ("hit", hpos)      # 同日双触发→miss
    elif hpos is not None:
        t, pos = "hit", hpos
    elif mpos is not None:
        t, pos = "miss", mpos
    else:
        t, pos = "flat", cfg["window"] - 1
    return {"type": t, "days": pos + 1, "max_up": float(hu.max()), "min_dn": float(ld.min())}


def lookup_path(ver, out_dir=None):
    return os.path.join(out_dir or C.LOOKUP_DIR, "lookup-%s-%s.json" % (ver, BUCKET_VER))


def build_lookup(parq_dir=None, out_dir=None, cfg_ver=None):
    """逐股流式回放全史信号→18桶统计(离线、一次性慢)。env=sh000300 close vs ma240。"""
    import kdj.layers as L
    from kdj.layers import DEFAULT_CFG
    parq_dir = parq_dir or C.PARQ_DIR
    out_dir = out_dir or C.LOOKUP_DIR
    cfg_ver = cfg_ver or C.cfg_version()
    os.makedirs(out_dir, exist_ok=True)
    sh = pd.read_parquet(os.path.join(parq_dir, "sh000300.parquet"))
    sh_bull = (sh["close"] > sh["ma240"])

    stat = {}
    for fp in sorted(glob.glob(os.path.join(parq_dir, "*.parquet"))):
        sym = os.path.basename(fp)[:-8]
        if not (sym.startswith("sh60") or sym.startswith("sz00")):
            continue                                   # 主板白名单
        df = pd.read_parquet(fp)
        if not {"high", "close", "volume", "kdj_j"}.issubset(df.columns):
            continue                                   # 旧版 parquet(无raw列)跳过
        g = df[["high", "low", "close", "volume"]].copy()
        g["j"] = df["kdj_j"]
        sig = L.signal(g, DEFAULT_CFG)
        if not sig.any():
            continue
        env = sh_bull.reindex(g.index).ffill().fillna(False)
        fs = feature_series(g)
        for i in np.flatnonzero(sig.values):
            o = replay_outcome(g, i)
            if o is None:
                continue
            key = bucket_key(bool(env.iloc[i]), fs["j_low"].iloc[i], fs["dd"].iloc[i])
            st = stat.setdefault(key, {"n": 0, "hit": 0, "sum_mu_hit": 0.0,
                                       "sum_md_miss": 0.0, "types": {}})
            st["n"] += 1
            st["types"][o["type"]] = st["types"].get(o["type"], 0) + 1
            if o["type"] == "hit":
                st["hit"] += 1
                st["sum_mu_hit"] += o["max_up"]
            if o["type"] == "miss":
                st["sum_md_miss"] += o["min_dn"]

    buckets = {}
    for k, st in stat.items():
        hits, misses = st["hit"], st["types"].get("miss", 0)
        payoff = ((st["sum_mu_hit"] / hits) / abs(st["sum_md_miss"] / misses)
                  if hits and misses else None)
        buckets["|".join(k)] = {"n": st["n"], "win_rate": st["hit"] / st["n"],
                                "payoff": payoff, "types": st["types"]}
    out = {"cfg_version": cfg_ver, "buckets": buckets}
    with open(lookup_path(cfg_ver, out_dir), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def load_lookup(ver):
    p = lookup_path(ver)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None
