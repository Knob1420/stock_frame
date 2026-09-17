# -*- coding: utf-8 -*-
import json

import pandas as pd
import pytest

from pool import lookup as K
from pool.cfg import cfg_version
from pool.tests.conftest import make_df


def test_bucket_key_and_clamp():
    assert K.bucket_key(True, 8.0, -0.10) == ("bull", "j<=10", "dd>-15")
    assert K.bucket_key(False, 13.0, -0.20) == ("bear", "10<j<=15", "dd<=-15")
    assert K.bucket_key(True, -30.0, -0.60) == ("bull", "j<=10", "dd<=-15")   # 超界钳制到边缘桶
    assert K.bucket_key(False, 40.0, 0.05) == ("bear", "10<j<=15", "dd>-15")  # 超上界同钳制


def test_replay_outcome_paths():
    n = 30
    idx = pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d")
    base = {"open": [10.0] * n, "high": [10.0] * n, "low": [10.0] * n, "close": [10.0] * n,
            "volume": [1e7] * n, "j": [50.0] * n}
    df = pd.DataFrame(base, index=idx)
    df.iloc[11, df.columns.get_loc("high")] = 10.9                       # T+1 触 +9% → hit
    assert K.replay_outcome(df, 10)["type"] == "hit"
    df2 = df.copy(); df2.iloc[11, df2.columns.get_loc("low")] = 9.4      # T+1 触 −6% → miss
    assert K.replay_outcome(df2, 10)["type"] == "miss"
    df3 = df.copy()                                                      # 全程 ±0.5% → flat
    for i in range(11, 21):
        df3.iloc[i, df3.columns.get_loc("high")] = 10.05
        df3.iloc[i, df3.columns.get_loc("low")] = 9.96
    assert K.replay_outcome(df3, 10)["type"] == "flat"
    assert K.replay_outcome(df3, 29) is None                             # 尾部窗口不足


def test_replay_same_day_double_is_miss():
    n = 30
    idx = pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d")
    df = pd.DataFrame({"open": [10.0] * n, "high": [10.0] * n, "low": [10.0] * n,
                       "close": [10.0] * n, "volume": [1e7] * n, "j": [50.0] * n}, index=idx)
    df.iloc[11, df.columns.get_loc("high")] = 10.9
    df.iloc[11, df.columns.get_loc("low")] = 9.4
    assert K.replay_outcome(df, 10)["type"] == "miss"


def test_build_lookup_versioned(tmp_path, monkeypatch):
    # 合成 2 只股票 + 1 只指数;signal 打桩为"第 10 行与倒数第 11 行为 True"
    import numpy as np
    import kdj.layers as L
    parq = tmp_path / "ind"; parq.mkdir()
    sh = make_df(n=600); sh["close"] = sh["close"] * 0 + 100.0           # 指数横盘
    sh.to_parquet(parq / "sh000300.parquet")
    for sym in ("sh600001", "sz000002"):
        make_df(seed=hash(sym) % 1000).to_parquet(parq / ("%s.parquet" % sym))

    def fake_signal(df, cfg):
        s = pd.Series(False, index=df.index)
        s.iloc[10] = True; s.iloc[-11] = True
        return s
    monkeypatch.setattr(L, "signal", fake_signal)
    out = K.build_lookup(parq_dir=str(parq), out_dir=str(tmp_path), cfg_ver="kdj-default-TEST")
    p = tmp_path / "lookup-kdj-default-TEST.json"
    assert p.is_file()
    data = json.load(open(p))
    assert data["cfg_version"] == "kdj-default-TEST"
    total = sum(b["n"] for b in data["buckets"].values())
    assert total == 4                                                    # 2股×2信号
    for b in data["buckets"].values():
        assert 0.0 <= b["win_rate"] <= 1.0
        assert set(b["types"]) <= {"hit", "miss", "flat"}
    # 换版本重生成 → 不同文件,旧文件保留(隔离)
    K.build_lookup(parq_dir=str(parq), out_dir=str(tmp_path), cfg_ver="kdj-default-V2")
    assert (tmp_path / "lookup-kdj-default-V2.json").is_file() and p.is_file()
