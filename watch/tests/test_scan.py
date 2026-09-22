# -*- coding: utf-8 -*-
"""save_briefs 落盘结构测试(不触网)。"""
import json

from scan import save_briefs


def test_save_briefs_roundtrip(tmp_path):
    events = [
        {"code": "sh600519", "rule": "b1", "close": 1420.0, "thesis": "缩量回踩低吸"},
        {"code": "sh600519", "rule": "near_ma(MA120)", "close": 1420.0, "thesis": "缩量回踩低吸"},
        {"code": "sz000333", "rule": "weekly_b1", "close": 68.5, "thesis": ""},
    ]
    briefs = {"sh600519": "①支撑\n②压力\n③量价\n④位置\n⑤符合低吸前置状态\n⑥风险",
              "sz000333": "(该票分析失败: x)"}
    p = save_briefs("2026-09-22", briefs, events, out_dir=str(tmp_path))
    data = json.load(open(p, encoding="utf-8"))
    assert data["date"] == "2026-09-22"
    assert data["briefs"]["sh600519"]["rules"] == "b1+near_ma(MA120)"   # 同票多规则合并
    assert data["briefs"]["sh600519"]["close"] == 1420.0
    assert data["briefs"]["sh600519"]["thesis"] == "缩量回踩低吸"
    assert "⑤符合低吸前置状态" in data["briefs"]["sh600519"]["brief"]
    assert data["briefs"]["sz000333"]["rules"] == "weekly_b1"


def test_save_briefs_noop(tmp_path):
    assert save_briefs("2026-09-22", {}, [], out_dir=str(tmp_path)) is None
    assert not (tmp_path / "2026-09-22.json").exists()


def test_market_scan_majority_date(tmp_path):
    """基准指数滞后个股一天时,数据日取个股多数派末行日期,滞后/陈旧文件不入统计。"""
    import pandas as pd
    from scan import _market_scan
    ind = tmp_path / "ind"
    ind.mkdir()

    def wr(sym, closes, end):
        idx = pd.bdate_range(end=end, periods=len(closes))
        pd.DataFrame({"close": closes, "volume": 100.0, "vma20": 50.0},
                     index=idx).to_parquet(ind / ("%s.parquet" % sym))

    up = [10.0] * 250 + [12.0, 13.0, 14.0]          # 涨 & 站上MA250
    dn = [10.0] * 250 + [9.0, 8.0, 7.0]             # 跌 & 年线下
    for s in ("sz000001", "sz000002"):
        wr(s, up, "2026-09-22")
    wr("sz000003", dn, "2026-09-22")
    wr("sz000009", up, "2026-09-21")                # 陈旧(退市残留),应排除
    wr("sh000300", [100.0] * 30, "2026-09-21")      # 基准指数滞后一天
    st = _market_scan(ind_dir=str(ind))
    assert st["date"] == "2026-09-22"               # 多数派个股日期,非基准指数日期
    assert (st["adv"], st["dec"], st["flat"]) == (2, 1, 0)
    assert st["total"] == 3 and st["above"] == 2    # 陈旧票不计入
    assert st["med_vr"] == 2.0
    assert "bench_d5" in st                          # 基准指数仍供自身数值
