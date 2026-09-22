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
