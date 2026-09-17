# -*- coding: utf-8 -*-
"""scan.py —— 每日确定性管道:信号复算→硬过滤→桶排序→池内跟踪→日报(spec §4)。
唯一数据源=indicators parquet(路线B);layers.signal 原封复用,尾部策略滚动现场算。
本文件=筛选核心(assemble/iter_whitelist/env_label/collect_candidates/screen);日报/CLI 在 Task 6。"""
import glob
import os
import statistics

import pandas as pd

from pool import cfg as C
from pool.lookup import bucket_key, feature_series          # noqa: F401(bucket_key 供 main_scan 标注)

RAW_COLS = ["open", "high", "low", "close", "volume"]
MIN_AMOUNT = 2e8          # 流动性:当日成交额(真实,元)
MAX_CHG = 0.097           # 收盘涨停代理(≥9.7% 剔除,次日大概率买不进)
TAIL = 500                # 回看窗口(MA240+EMA收敛余量)


def assemble(df):
    """parquet → layers 输入:原始5列 + j=kdj_j(同批量口径,零重算)。"""
    g = df[RAW_COLS].copy()
    g["j"] = df["kdj_j"]
    return g


def iter_whitelist(parq_dir=None):
    parq_dir = parq_dir or C.PARQ_DIR
    for fp in sorted(glob.glob(os.path.join(parq_dir, "*.parquet"))):
        sym = os.path.basename(fp)[:-8]
        if sym.startswith(("sh60", "sz00")):
            yield fp, sym


def env_label(parq_dir=None):
    """sh000300 vs MA240 → (bull, 指数末日, 指数日涨跌)。指数数据可能滞后一天(已知)。"""
    parq_dir = parq_dir or C.PARQ_DIR
    sh = pd.read_parquet(os.path.join(parq_dir, "sh000300.parquet"))
    bull = bool(sh["close"].iloc[-1] > sh["ma240"].iloc[-1])
    chg = float(sh["close"].iloc[-1] / sh["close"].iloc[-2] - 1)
    return bull, sh.index[-1], chg


def collect_candidates(parq_dir=None, expected_date=None, signal_fn=None, tail=TAIL):
    """复算当日信号并提取特征。signal_fn 可注入(测试);expected_date 非 None 时
    末日不符的票记为异常(停牌/滞后)。返回 (cands, anomalies)。"""
    import kdj.layers as L
    from kdj.layers import DEFAULT_CFG
    parq_dir = parq_dir or C.PARQ_DIR
    sig_fn = signal_fn or (lambda g, cfg: L.signal(g, cfg))
    cands, anomalies = [], []
    for fp, sym in iter_whitelist(parq_dir):
        try:
            df = pd.read_parquet(fp)
            if expected_date and df.index[-1] != expected_date:
                anomalies.append((sym, "末日 %s ≠ %s(停牌/滞后)" % (df.index[-1], expected_date)))
                continue
            g = assemble(df.tail(tail))
            sig = sig_fn(g, DEFAULT_CFG)
            if not bool(sig.iloc[-1]):
                continue
            prev = g["close"].iloc[-2]
            cur = g["close"].iloc[-1]
            fs = feature_series(g)
            cands.append({"sym": sym, "date": g.index[-1], "close": float(cur),
                          "prev_close": float(prev), "chg": float(cur / prev - 1),
                          "amount": float(df["amount"].iloc[-1] / df["factor"].iloc[-1] * 1000),
                          "feats": {k: (float(s.iloc[-1]) if s.iloc[-1] == s.iloc[-1] else None)
                                    for k, s in fs.items()}})
        except Exception as e:                                  # 单票异常不炸整体
            anomalies.append((sym, repr(e)))
    return cands, anomalies


def screen(cands, lookup_data, top=30, min_amount=MIN_AMOUNT, max_chg=MAX_CHG):
    """硬过滤(成交额/涨停代理)+ 同型桶胜率排序(spec §6)。返回 (top_list, 剔除数)。
    n≥30 桶用其 win_rate;<30/缺桶置全部达标桶 win_rate 的中位数(不加分)。
    排序:score 降序 → 有达标桶统计者在前 → 并列按成交额降序(中位分不应挤掉真实桶同分票)。"""
    alive = [c for c in cands if c["amount"] >= min_amount and c["chg"] < max_chg]
    n_rej = len(cands) - len(alive)
    tbl = lookup_data.get("buckets", {})
    scores = [b["win_rate"] for b in tbl.values() if b.get("n", 0) >= 30]
    med = statistics.median(scores) if scores else 0.5         # <30 样本桶置中位不加分

    def qualified(c):
        st = c.get("bucket_stat")
        return st is not None and st.get("n", 0) >= 30

    for c in alive:
        c["bucket_stat"] = tbl.get(c["bucket"])
        n = c["bucket_stat"]["n"] if c["bucket_stat"] else 0
        c["score"] = c["bucket_stat"]["win_rate"] if c["bucket_stat"] and n >= 30 else med
    alive.sort(key=lambda c: (-c["score"], 0 if qualified(c) else 1, -c["amount"]))
    return alive[:top], n_rej
