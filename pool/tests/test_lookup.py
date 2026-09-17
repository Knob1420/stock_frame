# -*- coding: utf-8 -*-
import json

import pandas as pd
import pytest

from pool import lookup as K
from pool.cfg import cfg_version
from pool.tests.conftest import make_df


def test_bucket_key_and_clamp():
    # 6c: J 三档(≤5 / 5~10 / 10~15) × dd 三档(>-10% / -10~-15% / -15~-25%),边界值归更深档
    assert K.bucket_key(True, 5.0, -0.05) == ("bull", "j<=5", "dd>-10")        # J=5 落 j<=5
    assert K.bucket_key(True, 7.0, -0.05) == ("bull", "5<j<=10", "dd>-10")
    assert K.bucket_key(True, 10.0, -0.05) == ("bull", "5<j<=10", "dd>-10")    # J=10 落 5<j<=10
    assert K.bucket_key(True, 12.0, -0.05) == ("bull", "10<j<=15", "dd>-10")
    assert K.bucket_key(False, 8.0, -0.099) == ("bear", "5<j<=10", "dd>-10")
    assert K.bucket_key(False, 8.0, -0.10) == ("bear", "5<j<=10", "-15<dd<=-10")   # dd=-10% 落中档
    assert K.bucket_key(False, 8.0, -0.149) == ("bear", "5<j<=10", "-15<dd<=-10")
    assert K.bucket_key(False, 8.0, -0.15) == ("bear", "5<j<=10", "dd<=-15")       # dd=-15% 落深档
    assert K.bucket_key(False, 8.0, -0.20) == ("bear", "5<j<=10", "dd<=-15")
    # 超界钳制到边缘桶(双向:J<0 与 J>15、dd 超下界与超上界)
    assert K.bucket_key(True, -30.0, -0.60) == ("bull", "j<=5", "dd<=-15")
    assert K.bucket_key(False, 40.0, 0.05) == ("bear", "10<j<=15", "dd>-10")


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


def test_replay_skips_limitup_signal_day():
    n = 30
    idx = pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d")
    df = pd.DataFrame({"open": [10.0] * n, "high": [10.0] * n, "low": [10.0] * n,
                       "close": [10.0] * n, "volume": [1e7] * n, "j": [50.0] * n}, index=idx)
    df.iloc[11, df.columns.get_loc("high")] = 10.9                       # T+1 +9% → 本应 hit
    assert K.replay_outcome(df, 10)["type"] == "hit"                     # 对照:普通信号日照常回放
    df.iloc[10, df.columns.get_loc("close")] = 11.0                      # 信号日收盘 +10% → 跳过
    assert K.replay_outcome(df, 10) is None
    # 恰 9.700% 无法浮点精确表达(10.97/10−1 < 0.097,筛选同式同判),以 ±0.01% 夹逼阈值
    df.iloc[10, df.columns.get_loc("close")] = 10.971                    # +9.71%(阈上方)→ 同剔,≥ 口径
    assert K.replay_outcome(df, 10) is None
    df.iloc[10, df.columns.get_loc("close")] = 10.969                    # +9.69%(阈下方)→ 恢复回放
    assert K.replay_outcome(df, 10) is not None


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
    p = tmp_path / "lookup-kdj-default-TEST-b2.json"                     # 6c: 文件名含桶方案版本
    assert p.is_file()
    assert not (tmp_path / "lookup-kdj-default-TEST.json").exists()      # 旧命名不再产生
    data = json.load(open(p))
    assert data["cfg_version"] == "kdj-default-TEST"
    total = sum(b["n"] for b in data["buckets"].values())
    assert total == 4                                                    # 2股×2信号
    for b in data["buckets"].values():
        assert 0.0 <= b["win_rate"] <= 1.0
        assert set(b["types"]) <= {"hit", "miss", "flat"}
    # 换版本重生成 → 不同文件,旧文件保留(隔离)
    K.build_lookup(parq_dir=str(parq), out_dir=str(tmp_path), cfg_ver="kdj-default-V2")
    assert (tmp_path / "lookup-kdj-default-V2-b2.json").is_file() and p.is_file()


def test_build_lookup_excludes_limitup_signals(tmp_path, monkeypatch):
    # 6c: 信号日收盘涨幅 ≥9.7%(涨停代理)不入统计——回放与筛选口径对称
    import kdj.layers as L
    parq = tmp_path / "ind"; parq.mkdir()
    sh = make_df(n=600); sh["close"] = sh["close"] * 0 + 100.0
    sh.to_parquet(parq / "sh000300.parquet")
    for sym in ("sh600001", "sz000002"):
        d = make_df(seed=hash(sym) % 1000)
        d.iloc[10, d.columns.get_loc("close")] = d.iloc[9, d.columns.get_loc("close")] * 1.10   # 信号日涨停
        d.to_parquet(parq / ("%s.parquet" % sym))

    def fake_signal(df, cfg):
        s = pd.Series(False, index=df.index)
        s.iloc[10] = True; s.iloc[-11] = True
        return s
    monkeypatch.setattr(L, "signal", fake_signal)
    K.build_lookup(parq_dir=str(parq), out_dir=str(tmp_path), cfg_ver="kdj-default-TEST")
    data = json.load(open(tmp_path / "lookup-kdj-default-TEST-b2.json"))
    total = sum(b["n"] for b in data["buckets"].values())
    assert total == 2                                                    # 2股×1信号:第10行涨停信号被剔


def test_load_lookup_ignores_legacy_no_suffix(tmp_path, monkeypatch):
    # 6c: 旧 8 桶文件(无 -b2 后缀)不被新代码加载;新方案文件正常读
    from pool import cfg as C
    monkeypatch.setattr(C, "LOOKUP_DIR", str(tmp_path))
    (tmp_path / "lookup-v9.json").write_text(
        json.dumps({"cfg_version": "v9", "buckets": {"bull|j<=10|dd>-15": {"n": 99}}}),
        encoding="utf-8")
    assert K.load_lookup("v9") is None
    (tmp_path / "lookup-v9-b2.json").write_text(
        json.dumps({"cfg_version": "v9", "buckets": {"bull|j<=5|dd>-10": {"n": 1}}}),
        encoding="utf-8")
    assert K.load_lookup("v9")["buckets"]["bull|j<=5|dd>-10"]["n"] == 1


def test_limit_up_constants_locked_to_screen():
    # 6c 评审修复: 涨停口径对称裁决使 LIM_UP == MAX_CHG 成为载荷不变式(两常量分居
    # lookup/scan,循环导入阻碍共享,cfg.py 不在本任务文件清单)——以测试锁定,改其一即红
    from pool.scan import MAX_CHG
    from pool.lookup import LIM_UP
    assert LIM_UP == MAX_CHG
