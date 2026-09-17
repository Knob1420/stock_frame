# -*- coding: utf-8 -*-
"""build_indicators 新指标单测:ma/vma/boll/atr 纯函数 + 注册表一致性。
口径:MA 族 min_periods=n(不足 NaN);BOLL=样本标准差(ddof=1,通达信口径);
ATR=MA(TR,14) 国内口径(非 Wilder);首 bar TR=high-low(REF 缺失取可得项)。
NaN 断言用 isna()(pandas 3.0 的 tolist() NaN 之间不相等)。"""
import numpy as np
import pandas as pd
import pytest

import build_indicators as bi
from indicators_conf import INDICATORS


def test_ma_windows_and_min_periods():
    close = pd.Series([1.0, 2, 3, 4, 5, 6])
    out = bi.ma(close, windows=[3, 5])
    assert list(out.columns) == ["ma3", "ma5"]
    assert out["ma3"].isna().tolist() == [True, True, False, False, False, False]
    assert out["ma3"].tolist()[2:] == [2.0, 3.0, 4.0, 5.0]
    assert out["ma5"].isna().tolist() == [True] * 4 + [False] * 2
    assert out["ma5"].tolist()[4:] == [3.0, 4.0]


def test_vma_on_volume():
    vol = pd.Series([10.0, 20, 30, 40])
    out = bi.vma(vol, windows=[2])
    assert out["vma2"].isna().tolist() == [True, False, False, False]
    assert out["vma2"].tolist()[1:] == [15.0, 25.0, 35.0]


def test_boll_mid_and_sample_std():
    close = pd.Series(range(1, 21), dtype=float)           # 1..20
    out = bi.boll(close, n=20, p=2)
    assert list(out.columns) == ["boll_upper", "boll_mid", "boll_lower"]
    assert out["boll_mid"].isna().iloc[:19].all()          # 不足 20 根 → NaN
    upper, mid, lower = out.iloc[-1]                       # 列序即解包序
    x = np.arange(1, 21, dtype=float)
    assert mid == pytest.approx(x.mean())                  # 10.5
    sd = x.std(ddof=1)                                     # 样本标准差(通达信口径)
    assert upper == pytest.approx(mid + 2 * sd)
    assert lower == pytest.approx(mid - 2 * sd)


def test_atr_domestic_ma_of_true_range():
    high = pd.Series([10.0, 11, 12])
    low = pd.Series([9.0, 10, 10.5])
    close = pd.Series([9.5, 10.5, 11.5])
    out = bi.atr(high, low, close, n=2)
    # TR1=10-9=1(无昨收);TR2=max(1,1.5,0.5)=1.5;TR3=max(1.5,1.5,0)=1.5
    assert out["atr2"].isna().tolist() == [True, False, False]
    assert out["atr2"].tolist()[1:] == [(1.0 + 1.5) / 2, (1.5 + 1.5) / 2]


def test_registry_matches_funccs_output():
    """注册表 columns 与各 FUNCS 实际输出一致(防注册表漂移);需要的 $ 字段齐备。"""
    n = 30
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    g = pd.DataFrame({
        "$open": np.linspace(1, 2, n), "$high": np.linspace(2, 3, n),
        "$low": np.linspace(0.5, 1.5, n), "$close": np.linspace(1, 2, n),
        "$volume": np.linspace(100, 200, n)}, index=idx)
    for name in INDICATORS:
        assert name in bi.FUNCS, "FUNCS 缺实现: %s" % name
        out = bi.FUNCS[name](g)
        assert list(out.columns) == INDICATORS[name]["columns"], name
