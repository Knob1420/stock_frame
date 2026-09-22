# -*- coding: utf-8 -*-
"""report_html 渲染测试:骨架+①④区、base64 内嵌、index 生成。"""
from report_html import build
from util import PNG1x1

MARKET = {"date": "2026-09-21", "adv": 2000, "dec": 3000, "flat": 500, "above": 2200,
          "total": 5500, "med_vr": 0.95, "bench_d5": -0.01, "bench_d20": 0.02,
          "bench_dist250": 0.03}


def test_build_market_tracking_index(tmp_path):
    png = tmp_path / "market.png"
    png.write_bytes(PNG1x1)
    p = build(events=[], tracking=[{"code": "sh600519", "name": "贵州茅台", "rule": "b1", "days": 3}],
              briefs={}, charts={"market": str(png)}, market=MARKET,
              date="2026-09-21", out_dir=str(tmp_path))
    html = open(p, encoding="utf-8").read()
    assert "盯盘日报 2026-09-21" in html
    assert "涨2000" in html and "跌3000" in html                          # ①区数字
    assert "距年线 +3.0%" in html and "近5日 -1.0%" in html
    assert "data:image/png;base64," in html                               # 图内嵌
    assert "板块数据待接入" in html                                       # ②区留位
    assert "sh600519" in html and "b1" in html and "3" in html            # ④区跟踪
    idx = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert 'href="2026-09-21.html"' in idx                                # 索引链接


def test_build_no_chart_no_crash(tmp_path):
    p = build(events=[], tracking=[], briefs={}, charts={}, market={},
              date="2026-09-21", out_dir=str(tmp_path))
    html = open(p, encoding="utf-8").read()
    assert "大盘图未生成" in html and "统计不可用" not in html              # 降级不崩
