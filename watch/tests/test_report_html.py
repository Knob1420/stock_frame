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


def test_backfill_and_qa_sections(tmp_path):
    import json
    import pandas as pd
    from util import write_px
    ind = tmp_path / "ind"
    px = [10, 10.5, 11, 10.8, 10.5, 10.2, 10.6, 11.2, 11.5, 12.0, 11.8, 10.9]
    idx = write_px(ind / "sz000001.parquet", px)
    d5 = str(idx[6].date())          # 触发后至今=5个交易日 → milestone
    pd.DataFrame([{"code": "sz000001", "name": "测试", "rule": "b1", "date": d5, "close": 10.6}]
                 ).to_parquet(tmp_path / "archive.parquet")
    bd = tmp_path / "briefs"
    bd.mkdir()
    json.dump({"date": d5, "briefs": {"sz000001": {
        "rules": "b1", "close": 10.6, "thesis": "", "brief": "①a\n②b\n③c\n④d\n⑤回踩企稳可观察\n⑥e"}}},
        open(bd / ("%s.json" % d5), "w", encoding="utf-8"), ensure_ascii=False)
    qd = tmp_path / "qa"
    qd.mkdir()
    json.dump({"date": "2026-09-21", "qa": [{"code": "sz000001", "q": "地量标准?",
                                             "a": "量比<0.5 且低于250日20分位", "ts": "21:30"}]},
              open(qd / "2026-09-21.json", "w", encoding="utf-8"), ensure_ascii=False)
    p = build(events=[], tracking=[], briefs={}, charts={}, market=MARKET, date="2026-09-21",
              out_dir=str(tmp_path), archive_f=str(tmp_path / "archive.parquet"), ind_dir=str(ind))
    html = open(p, encoding="utf-8").read()
    assert "⑤ 历史回填" in html and d5 in html
    assert 'class="ms"' in html and "满5日" in html              # milestone 高亮
    assert "回踩企稳可观察" in html                               # LLM当时判定来自briefs JSON
    assert "⑥ 追问记录" in html and "地量标准?" in html and "量比<0.5" in html
    assert "+2.8%" in html                                       # ret_now=10.9/10.6-1(现价口径)


def test_backfill_empty(tmp_path):
    p = build(events=[], tracking=[], briefs={}, charts={}, market=MARKET, date="2026-09-21",
              out_dir=str(tmp_path), archive_f=str(tmp_path / "none.parquet"), ind_dir=str(tmp_path))
    html = open(p, encoding="utf-8").read()
    assert "暂无可回填事件" in html and "今日无追问" in html


def test_cards_from_archive_arrays(tmp_path):
    """重建路径:archive parquet 回读的 kdj/macd/boll 是 ndarray,卡片渲染不得炸。"""
    import numpy as np
    ev = [_ev("sh600519", "贵州茅台")]
    ev[0]["kdj"] = np.array([18.0, 22.0, 6.0])
    ev[0]["macd"] = np.array([-0.1, -0.08, -0.02])
    ev[0]["boll"] = np.array([11.8, 11.0, 10.2])
    p = build(events=ev, tracking=[], briefs={}, charts={}, market=MARKET,
              date="2026-09-21", out_dir=str(tmp_path))
    html = open(p, encoding="utf-8").read()
    assert "sh600519 贵州茅台" in html and "18/22/6" in html


def test_rebuild_briefs_flat_text(tmp_path):
    """重建入口:briefs JSON(嵌套)须拍平为 {code: 六段文本} 喂卡片,dict 不能进 .strip()。"""
    import json
    from report_html import _load_brief_text
    bd = tmp_path / "briefs"
    bd.mkdir()
    json.dump({"date": "2026-09-22", "briefs": {"sh600519": {
        "rules": "b1", "close": 1420.0, "thesis": "",
        "brief": "①支撑\n②压力\n③量价\n④位置\n⑤符合低吸前置状态\n⑥风险"}}},
        open(bd / "2026-09-22.json", "w", encoding="utf-8"), ensure_ascii=False)
    flat = _load_brief_text("2026-09-22", str(tmp_path))
    assert flat == {"sh600519": "①支撑\n②压力\n③量价\n④位置\n⑤符合低吸前置状态\n⑥风险"}
    ev = [_ev("sh600519", "贵州茅台")]
    p = build(events=ev, tracking=[], briefs=flat, charts={}, market=MARKET,
              date="2026-09-22", out_dir=str(tmp_path))
    html = open(p, encoding="utf-8").read()
    assert "判定:⑤符合低吸前置状态" in html          # 卡头结论来自扁平文本
