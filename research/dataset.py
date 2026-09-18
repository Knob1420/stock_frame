# -*- coding: utf-8 -*-
"""dataset.py —— R0 研究底座(离线,不进日常链,零生产模块改动)。

一遍全扫白名单 parquet,产出**事件级研究数据集**:每个历史信号一行,
特征 × 环境快照 × 多期限结局 × 同日全市场基线。之后 R1/R2 的条件研究、
案例检索全部在此数据集上进行,不再反复扫 3000+ 只票。

口径(与生产一致,偏差见 viability 报告脚注):
- 前瞻收益为**仓位制**(该股自身 bar 序列第 h 根后,停牌照位)——同 replay_outcome/track
- 基线 = 同日全白名单横截面中位数(同样仓位制)
- 结局三判 = 池口径 CLOSE_CFG(hit+8%/miss-5%/flat 10日,T+1 起算)
- 环境窗口自 2006-01-01 起(指数 MA240 预热完成,env 标签才有效)

产物: research/dataset-<cfg_version>.parquet + 同名 .meta.json
"""
import glob
import gc
import json
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from kdj.layers import DEFAULT_CFG, signal                     # noqa: E402
from pool.cfg import cfg_version as _cfg_version               # noqa: E402
from pool.lookup import bucket_key, feature_series, replay_outcome   # noqa: E402
from pool.scan import assemble, iter_whitelist                 # noqa: E402

HORIZONS = [1, 3, 5, 10, 20]
ENV_START = "2006-01-01"        # 指数 ma240 预热完成
DEDUP_COOLDOWN = 5              # 同股信号间隔≤5根bar视为同一 episode
MIN_AMOUNT = 2e8                # 与生产 screen 一致
MAX_CHG = 0.097
WINDOW = 10                     # 池结案窗口
BASELINE_EVERY = 4              # 基线抽样:每 4 只取 1(内存约束,中位数估计仍稳;None=全量)
FLUSH_EVERY = 100000            # 事件分块落帧阈值
COLS = ["date", "sym", "cfg_version",
        "j_low", "dd", "age", "shrink", "vol_ratio", "amount", "chg",
        "env_bull", "idx_chg20", "breadth20", "bucket", "pass_filter", "ep_start",
        "r1", "r3", "r5", "r10", "r20",
        "b1", "b3", "b5", "b10", "b20",
        "outcome_type", "outcome_days", "mfe", "mae", "ft_up_day", "ft_dn_day"]


def _f(x):
    """标量 NaN(pandas/numpy)→python NaN,保持 float。"""
    x = float(x)
    return x if x == x else np.nan


def fwd_returns(close, horizons=HORIZONS):
    """仓位制前瞻收盘收益:h 根 bar 后;尾部不足→NaN。返回 {h: ndarray}。"""
    c = np.asarray(close, dtype="float64")
    out = {}
    for h in horizons:
        r = np.full(len(c), np.nan)
        if len(c) > h:
            r[:len(c) - h] = c[h:] / c[:len(c) - h] - 1.0
        out[h] = r
    return out


def first_touch(high, low, base, window=WINDOW, up=0.08, dn=-0.05):
    """T+1 起窗口内首次触线日(1 基);未触→NaN。high/low 需自信号日 i 起切片。"""
    hu = np.asarray(high)[1:window + 1] / base - 1.0
    ld = np.asarray(low)[1:window + 1] / base - 1.0

    def first(mask):
        idx = np.flatnonzero(mask)
        return int(idx[0] + 1) if len(idx) else np.nan
    return first(hu >= up), first(ld <= dn)


def episode_starts(pos, cooldown=DEDUP_COOLDOWN):
    """信号位置序列→episode 首信号标记(距上一信号>cooldown 根 bar 才算新事件)。"""
    out = np.ones(len(pos), dtype=bool)
    if len(pos) > 1:
        out[1:] = np.diff(pos) > cooldown
    return out


def _calendar_positions(dates, cal):
    """日期→主日历仓位;日历外(2005 前/指数滞后日)→-1。"""
    pos = np.searchsorted(cal, dates)
    pos = np.minimum(pos, len(cal) - 1)
    ok = cal[pos] == dates
    return np.where(ok, pos, -1)


def build_dataset(parq_dir=None, out_dir=None, cfg_ver=None, verbose=True):
    parq_dir = parq_dir or os.path.join(ROOT, "stockdata", "indicators")
    out_dir = out_dir or os.path.join(ROOT, "research")
    cfg_ver = cfg_ver or _cfg_version()
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()

    sh = pd.read_parquet(os.path.join(parq_dir, "sh000300.parquet"))
    cal = sh.index.to_numpy()
    n_cal = len(cal)
    env_bull = (sh["close"] > sh["ma240"])
    idx_chg20 = (sh["close"] / sh["close"].shift(20) - 1)

    files = [(fp, sym) for fp, sym in iter_whitelist(parq_dir)]
    n_st = len(files)
    n_bl = len(range(0, n_st, BASELINE_EVERY)) if BASELINE_EVERY else n_st
    if verbose:
        print("白名单 %d 只,主日历 %s→%s(%d 日),基线抽样 %d 只" %
              (n_st, cal[0], cal[-1], n_cal, n_bl), flush=True)

    # 基线矩阵:行=日历日,列=抽样股票(每 BASELINE_EVERY 只取 1,内存约束)
    base = {h: np.full((n_cal, n_bl), np.nan, dtype="float32") for h in HORIZONS}
    br20 = np.full((n_cal, n_bl), np.nan, dtype="float32")

    events = []
    frames = []          # 每 FLUSH_EVERY 事件落一帧,防列表内存峰值
    for j, (fp, sym) in enumerate(files):
        df = pd.read_parquet(fp)
        g = assemble(df)
        cpos = _calendar_positions(g.index.to_numpy(), cal)
        fr = fwd_returns(g["close"].to_numpy())

        # --- 基线/宽度贡献(抽样股票,不限信号;信号统计仍用全量) ---
        if BASELINE_EVERY is None or j % BASELINE_EVERY == 0:
            col = j // BASELINE_EVERY if BASELINE_EVERY else j
            m = cpos >= 0
            for h in HORIZONS:
                base[h][cpos[m], col] = fr[h][m].astype("float32")
            ma20 = df["ma20"].to_numpy(dtype="float64")
            above = np.where(np.isnan(ma20), np.nan,
                             (g["close"].to_numpy() > ma20).astype("float64"))
            br20[cpos[m], col] = above[m].astype("float32")

        # --- 信号事件 ---
        sig = signal(g, DEFAULT_CFG)
        if not sig.any():
            continue
        fs = feature_series(g)
        hi = g["high"].to_numpy(dtype="float64")
        lo = g["low"].to_numpy(dtype="float64")
        cl = g["close"].to_numpy(dtype="float64")
        vol = g["volume"].to_numpy(dtype="float64")
        amt = df["amount"].to_numpy(dtype="float64") * 1000.0   # 千元→元(Task7 口径)
        vma20 = df["vma20"].to_numpy(dtype="float64")
        dates = g.index.to_numpy()
        bull_al = env_bull.reindex(g.index).ffill().fillna(False).to_numpy()
        chg20_al = idx_chg20.reindex(g.index).ffill().to_numpy()
        pos_sig = np.flatnonzero(sig.to_numpy())
        eps = episode_starts(pos_sig)
        for k, i in enumerate(pos_sig):
            d = dates[i]
            if d < ENV_START:
                continue
            cp = cpos[i]
            o = replay_outcome(g, i)
            ft_up, ft_dn = first_touch(hi[i:i + WINDOW + 1], lo[i:i + WINDOW + 1], cl[i])
            chg = cl[i] / cl[i - 1] - 1.0 if i >= 1 else np.nan
            events.append({
                "date": d, "sym": sym, "cfg_version": cfg_ver,
                "j_low": _f(fs["j_low"].iloc[i]), "dd": _f(fs["dd"].iloc[i]),
                "age": _f(fs["age"].iloc[i]), "shrink": _f(fs["shrink"].iloc[i]),
                "vol_ratio": vol[i] / vma20[i] if vma20[i] == vma20[i] else np.nan,
                "amount": float(amt[i]), "chg": _f(chg),
                "env_bull": bool(bull_al[i]),
                "idx_chg20": _f(chg20_al[i]), "breadth20": np.nan,
                "bucket": "|".join(bucket_key(bool(bull_al[i]),
                                              _f(fs["j_low"].iloc[i]),
                                              _f(fs["dd"].iloc[i]))),
                "pass_filter": bool(amt[i] >= MIN_AMOUNT and chg == chg and chg < MAX_CHG),
                "ep_start": bool(eps[k]),
                **{"r%d" % h: _f(fr[h][i]) for h in HORIZONS},
                "outcome_type": o["type"] if o else None,
                "outcome_days": o["days"] if o else np.nan,
                "mfe": o["max_up"] if o else np.nan,
                "mae": o["min_dn"] if o else np.nan,
                "ft_up_day": ft_up, "ft_dn_day": ft_dn,
            })
        if verbose and (j + 1) % 500 == 0:
            print("  扫描 %d/%d 只,事件 %d,耗时 %.0fs" % (j + 1, n_st, len(events), time.time() - t0),
                  flush=True)
            gc.collect()
        del df, g
        if len(events) >= FLUSH_EVERY:
            frames.append(pd.DataFrame(events, columns=COLS))
            events = []

    # 基线中位数/宽度(逐日),回填到事件
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        base_med = {h: np.nanmedian(base[h], axis=1) for h in HORIZONS}
        breadth = np.nanmean(br20, axis=1)
    del base, br20

    ds = pd.concat(frames + [pd.DataFrame(events, columns=COLS)], ignore_index=True) \
        if (frames or events) else pd.DataFrame(columns=COLS)
    if len(ds):
        cpos_ev = _calendar_positions(ds["date"].to_numpy(), cal)
        ok = cpos_ev >= 0
        for h in HORIZONS:
            ds["b%d" % h] = np.where(ok, base_med[h][np.maximum(cpos_ev, 0)], np.nan)
        ds["breadth20"] = np.where(ok, breadth[np.maximum(cpos_ev, 0)], np.nan)

    out_pq = os.path.join(out_dir, "dataset-%s.parquet" % cfg_ver)
    ds.to_parquet(out_pq)
    meta = {"cfg_version": cfg_ver, "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "n_stocks": n_st, "n_events": len(ds), "env_start": ENV_START,
            "horizons": HORIZONS, "window": WINDOW, "dedup_cooldown": DEDUP_COOLDOWN,
            "min_amount": MIN_AMOUNT, "max_chg": MAX_CHG,
            "baseline": ("全白名单" if not BASELINE_EVERY
                         else "等距每%d只抽1(共%d只)" % (BASELINE_EVERY, n_bl))
                        + "同日横截面中位数(仓位制);信号统计用全量",
            "date_range": [str(ds["date"].min()), str(ds["date"].max())] if len(ds) else None,
            "elapsed_sec": round(time.time() - t0, 1)}
    with open(out_pq.replace(".parquet", ".meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    if verbose:
        print("完成: %d 事件 → %s (%.0fs)" % (len(ds), out_pq, time.time() - t0))
    return ds


if __name__ == "__main__":
    build_dataset()
