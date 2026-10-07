# -*- coding: utf-8 -*-
"""backfill 前向收益口径:fwd5/10/20 现价口径、不足期 None、MFE/MAE、milestone。"""
import pandas as pd
import pytest

from backfill import forward_stats
from util import write_px

PX = [10, 10.5, 11, 10.8, 10.5, 10.2, 10.6, 11.2, 11.5, 12.0, 11.8, 10.9]  # 12个交易日


def _arc(tmp_path, rows):
    af = tmp_path / "archive.parquet"
    pd.DataFrame(rows).to_parquet(af)
    return af


def test_forward_stats_main(tmp_path):
    ind = tmp_path / "ind"
    idx = write_px(ind / "sz000001.parquet", PX)
    af = _arc(tmp_path, [{"code": "sz000001", "name": "测试", "rule": "near_ma(MA120)",
                          "date": str(idx[2].date()), "close": 11.0}])
    st = forward_stats(archive_f=af, ind_dir=str(ind), horizons=(5, 10, 20))
    assert len(st) == 1
    r = st.iloc[0]
    assert r["code"] == "sz000001" and r["trig_close"] == 11.0
    assert r["elapsed"] == 9                                          # 触发日之后还有9个交易日
    assert r["fwd5"] == pytest.approx(11.2 / 11 - 1, abs=1e-4)        # factor后半段翻倍不影响现价口径
    assert r["fwd10"] is None                                         # 不足10日
    assert r["ret_now"] == pytest.approx(10.9 / 11 - 1, abs=1e-4)
    assert r["mfe"] == pytest.approx(12.0 * 1.02 / 11 - 1, abs=1e-4)  # 次日起最高现价×1.02
    assert r["mae"] == pytest.approx(10.2 * 0.98 / 11 - 1, abs=1e-4)
    assert r["milestone"] is None                                     # 9 不在 (5,10,20)


def test_milestone_and_fresh_trigger(tmp_path):
    ind = tmp_path / "ind"
    idx = write_px(ind / "sh600519.parquet", PX)
    af = _arc(tmp_path, [
        {"code": "sh600519", "name": "A", "rule": "b1", "date": str(idx[6].date()), "close": 10.6},
        {"code": "sh600519", "name": "A", "rule": "weekly_b1", "date": str(idx[11].date()), "close": 10.9},
    ])
    st = forward_stats(archive_f=af, ind_dir=str(ind))
    ms = st[st["rule"] == "b1"].iloc[0]
    fresh = st[st["rule"] == "weekly_b1"].iloc[0]
    assert ms["elapsed"] == 5 and ms["milestone"] == 5
    assert fresh["elapsed"] == 0 and fresh["milestone"] is None
    assert fresh["ret_now"] is None and fresh["fwd5"] is None


def test_empty_and_missing(tmp_path):
    assert forward_stats(archive_f=tmp_path / "none.parquet", ind_dir=str(tmp_path)).empty
    ind = tmp_path / "ind"
    idx = write_px(ind / "sz000001.parquet", PX)
    af = _arc(tmp_path, [
        {"code": "sz000001", "name": "X", "rule": "b1", "date": str(idx[2].date()), "close": 11.0},
        {"code": "sz999999", "name": "缺数据", "rule": "b1", "date": str(idx[2].date()), "close": 1.0},
    ])
    st = forward_stats(archive_f=af, ind_dir=str(ind))
    assert list(st["code"]) == ["sz000001"]                           # 缺 parquet 的票跳过不阻断
