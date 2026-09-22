# -*- coding: utf-8 -*-
"""report_html 渲染测试:骨架+①④区、base64 内嵌、index 生成。"""
from report_html import build, seg
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


def _ev(code, name, **kw):
    e = {"code": code, "name": name, "rule": "b1", "date": "2026-09-21", "thesis": "低吸",
         "close": 11.0, "dist_ma120": -0.01, "dist_ma250": 0.05, "dist_ma360": None,
         "dd_52w": -0.12, "pos_52w": 0.3, "volr": 0.55, "rsi14": 38.5,
         "kdj": [18.0, 22.0, 6.0], "macd": [-0.1, -0.08, -0.02],
         "boll": [11.8, 11.0, 10.2], "atr_pct": 1.8, "lo20": 10.2, "hi20": 11.9}
    e.update(kw)
    return e


BRIEF6 = "①10.2/10.5\n②11.8/11.9\n③缩量企稳\n④逆势偏弱\n⑤符合低吸前置状态\n⑥大盘走弱注意仓位"
BRIEF_BAD = "支撑10.2\n压力11.8\n量价一般"          # 无⑤编号 → 回退首行


def test_seg_extract_and_fallback():
    assert "符合低吸前置状态" in seg(BRIEF6, 5)
    assert seg(BRIEF6, 1).startswith("10.2")           # 去掉编号符
    assert seg(BRIEF_BAD, 5) == "支撑10.2"             # 容错回退首行
    assert seg("", 5) == ""


def test_cards_render(tmp_path):
    png = tmp_path / "sh600519.png"
    png.write_bytes(PNG1x1)
    events = [_ev("sh600519", "贵州茅台"), _ev("sh600519", "贵州茅台", rule="near_ma(MA120)"),
              _ev("sz000333", "美的集团", rsi14=55.0)]
    briefs = {"sh600519": BRIEF6, "sz000333": BRIEF_BAD}
    p = build(events=events, tracking=[], briefs=briefs,
              charts={"sh600519": str(png)}, market=MARKET, date="2026-09-21", out_dir=str(tmp_path))
    html = open(p, encoding="utf-8").read()
    assert "sh600519 贵州茅台" in html
    assert "b1+near_ma(MA120)" in html                       # 同票多规则合并
    assert "判定:⑤符合低吸前置状态" in html[:html.index("sz000333")]  # 关键结论取⑤段(在卡头)
    assert "判定:⑤支撑" not in html                          # 无⑤段快报不伪造判定行
    assert "今日触发 <small>2 只</small>" in html             # 按票计
    assert "<details><summary>LLM 六段快报</summary>" in html
    assert BRIEF6 in html                                    # 折叠区内完整六段
    assert "data:image/png;base64," in html                  # 个股图内嵌
    assert "sz000333 美的集团" in html and "图未生成" in html  # 无图票降级
    assert "RSI14" in html and "距MA120" in html             # 对比表表头
    assert "sh600519 本票" in html and "sz000333 美的集团" in html  # 对比行含同日其他触发票


def test_bench_row(tmp_path):
    from report_html import _bench_row
    import pandas as pd
    idx = pd.bdate_range("2026-06-01", periods=130)
    df = pd.DataFrame({"close": 10.0, "high": 10.2, "low": 9.8, "volume": 100.0,
                       "vma20": 100.0, "rsi_14": 50.0, "factor": 1.0}, index=idx)
    df.to_parquet(tmp_path / "sh000300.parquet")
    r = _bench_row(ind_dir=str(tmp_path))
    assert r is not None and r["name"] == "沪深300"
    assert r["d120"] == "+0.0%" and r["rsi"] == "50.00"     # 恒定价格:距MA120=0
    assert _bench_row(ind_dir=str(tmp_path / "none")) is None   # 缺文件 → None(对比表降级)
