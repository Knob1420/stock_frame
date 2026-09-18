# -*- coding: utf-8 -*-
import json

import numpy as np
import pandas as pd
import pytest

import research.dataset as RD
from research.tests.conftest import make_rdf, write_pq


def test_fwd_returns_positional_and_tail_nan():
    close = np.array([10.0, 11.0, 12.1, 13.31, 14.0, 15.0])
    fr = RD.fwd_returns(close, horizons=[1, 3])
    assert fr[1][0] == pytest.approx(0.1)
    assert fr[3][0] == pytest.approx(0.331, rel=1e-6)
    assert np.isnan(fr[1][-1])                       # 尾部不足→NaN
    assert len(fr[1]) == len(close)


def test_first_touch():
    base = 10.0
    high = np.array([base, 10.2, 10.9, 10.5, 11.0])  # 信号日 + 4 根(窗口取前 window 根)
    low = np.array([base, 9.9, 9.4, 9.95, 9.0])
    ft_up, ft_dn = RD.first_touch(high, low, base, window=4)
    assert ft_up == 2 and ft_dn == 2                 # 各自首次触线日(T+1 1基)
    ft_up2, ft_dn2 = RD.first_touch(np.array([base] * 5), np.array([base] * 5), base, window=4)
    assert np.isnan(ft_up2) and np.isnan(ft_dn2)     # 未触→NaN


def test_episode_starts():
    eps = RD.episode_starts(np.array([10, 11, 12, 20, 30]))
    assert eps.tolist() == [True, False, False, True, True]   # 间隔>5 才算新事件
    assert RD.episode_starts(np.array([5])).tolist() == [True]


FIRES = {"sh600001": [10, 11, 20, 590], "sz000002": [300], "sh600003": [50]}


def _fake_signal(df, cfg):
    """按 close 序列指纹区分股票打桩( dataset 直接 import signal,替换其模块绑定)。"""
    import pandas as pd
    s = pd.Series(False, index=df.index)
    cl = df["close"].to_numpy()
    hi = df["high"].to_numpy()
    key = None
    if abs(cl[-1] - 10.0) < 1e-9 and abs(hi[11] - 10.9) < 1e-9:
        key = "sh600001"
    elif abs(cl[-1] - 10.0 * 1.01 ** 599) < 1e-6:
        key = "sz000002"
    elif abs(cl[50] / cl[49] - 1.10) < 1e-9:
        key = "sh600003"
    for r in FIRES.get(key, []):
        s.iloc[r] = True
    return s


def _mk_env(tmp_path, monkeypatch):
    """3 只股 + 指数,返回 (parq目录, a, b, c)。A 恒平+行11高点10.9;B 每根+1%;C 行50涨停代理。"""
    parq = tmp_path / "ind"
    sh = make_rdf(n=600, seed=99)
    sh["close"] = 100.0
    sh["ma240"] = 90.0                               # 恒牛
    write_pq(parq / "sh000300.parquet", sh)

    a = make_rdf(seed=1)
    a["close"] = 10.0
    a["open"] = 10.0
    a["low"] = 10.0
    a["high"] = 10.0
    a["volume"] = 3e7                                # amount=3e8 元,稳过流动性线
    a.loc[a.index[11], "high"] = 10.9                # 行10信号的 T+1 触 +9% → hit
    write_pq(parq / "sh600001.parquet", a)
    b = make_rdf(seed=2)
    b["close"] = 10.0 * 1.01 ** np.arange(600)       # B 每根 +1%
    write_pq(parq / "sz000002.parquet", b)
    c = make_rdf(seed=3)
    c.loc[c.index[50], "close"] = c["close"].iloc[49] * 1.10   # C 行50 涨停代理
    write_pq(parq / "sh600003.parquet", c)

    monkeypatch.setattr(RD, "signal", _fake_signal)
    return parq, a, b, c


def test_build_dataset_e2e(tmp_path, monkeypatch):
    parq, a, b, c = _mk_env(tmp_path, monkeypatch)
    monkeypatch.setattr(RD, "BASELINE_EVERY", None)   # 测试仅 3 只,禁用抽样
    out = tmp_path / "out"
    ds = RD.build_dataset(parq_dir=str(parq), out_dir=str(out), cfg_ver="TEST", verbose=False)
    assert len(ds) == 6 and set(ds["sym"]) == {"sh600001", "sz000002", "sh600003"}
    assert (ds["cfg_version"] == "TEST").all()

    ea = ds[ds["sym"] == "sh600001"].sort_values("date").reset_index(drop=True)
    # episode:行10/11/20/590 → 10 首发,11 距10=1→续,20 距11=9→新,590 距20=570→新
    assert ea["ep_start"].tolist() == [True, False, True, True]

    r0 = ea.iloc[0]                                  # 行10:T+1 high=10.9 → hit 首触日=1
    assert r0["outcome_type"] == "hit" and r0["ft_up_day"] == 1
    assert r0["mfe"] == pytest.approx(0.09, rel=1e-6)
    assert bool(r0["pass_filter"])

    assert pd.isna(ea.iloc[3]["outcome_type"])       # 行590 窗口不足 → 无结局
    assert np.isnan(ea.iloc[3]["r10"])               # 前瞻越界 → NaN

    # 基线 = 同日全白名单(A/B/C)仓位制 fwd 的中位数,手算对照
    i = 10
    vals = [0.0,                                     # A 恒平
            1.01 ** 10 - 1,                          # B
            float(c["close"].iloc[i + 10] / c["close"].iloc[i] - 1)]   # C(含行50跳变后的路径)
    assert r0["b10"] == pytest.approx(float(np.median(vals)), rel=1e-6)

    # pass_filter:C 行50 涨停代理(≥9.7%) → False
    ec = ds[ds["sym"] == "sh600003"].iloc[0]
    assert ec["chg"] == pytest.approx(0.10, rel=1e-6) and not bool(ec["pass_filter"])

    meta = json.load(open(out / "dataset-TEST.meta.json"))
    assert meta["n_events"] == 6 and meta["cfg_version"] == "TEST"
