# 盯盘四层展示一期（HTML 日报壳 + 快报 JSON + 回填区）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 watch/ 盯盘系统新增层2（静态 HTML 日报）与层4展示端（触发事件前向结局回填），并把 LLM 六段快报结构化落盘。

**Architecture:** 新增三个模块：`backfill.py`（archive 事件 → 前向收益，纯计算）、`report_html.py`（Jinja2 渲染单文件自包含日报 + index.html）；`scan.py` 最小侵入三处改动（快报落盘 JSON、末尾调渲染、企微附详情路径）。六区日报结构：①大盘 ②板块留位 ③触发卡片 ④跟踪中 ⑤历史回填 ⑥追问占位。

**Tech Stack:** Python 3（`.venv`）、pandas 3.0.6、pyarrow、Jinja2 3.1.6、pytest 9.1.1。全部已安装，**禁新增第三方依赖**。

**Spec:** `docs/superpowers/specs/2026-09-22-watch-html-daily-report-design.md`

## Global Constraints

- Python 一律用 `/home/admin/stock_frame/.venv/bin/python`（下文 `$PY`）；测试跑法 `(cd watch && ../.venv/bin/python -m pytest tests/ -v)`
- 现价口径：一切展示价格 = 后复权价 / factor（当日 factor）
- 单文件自包含 HTML：图片 base64 内嵌、CSS 内联 `<style>`、零外链零 JS 依赖
- 输出目录一律 `watch/reports/`（HTML/index/briefs/qa/charts 都在这里）
- 文件头 `# -*- coding: utf-8 -*-`，中文注释，风格对齐 watch/scan.py（紧凑、行尾注释说明口径）
- scan.py 的 `--push` 推送语义不得回归（只在报告文本末尾追加一行）
- 对 spec 的一处展示修正（已获用户同意方向）：⑤回填区默认只列 milestone 行（满 5/10/20 交易日），`<details>` 折叠全量表——archive 3 天已积 323 事件，全量平铺不可读
- backfill 不读 archive 的 mav/dist_ma 列（只用 close/high/low/factor），故天然免疫新旧均线列名混存问题

---

### Task 1: backfill.py 前向收益计算

**Files:**
- Create: `watch/backfill.py`
- Create: `watch/tests/conftest.py`
- Create: `watch/tests/util.py`
- Test: `watch/tests/test_backfill.py`

**Interfaces:**
- Consumes: `watch/archive.parquet`（列含 code/name/rule/date/close）、`stockdata/indicators/<code>.parquet`（列 close/high/low/factor，DatetimeIndex）
- Produces: `forward_stats(archive_f=ARCHIVE_F, ind_dir=IND, lookback=20, horizons=(5,10,20)) -> pd.DataFrame`，列固定为 `["code","name","rule","date","trig_close","elapsed","ret_now","fwd5","fwd10","fwd20","mfe","mae","milestone"]`；`ARCHIVE_F`/`IND`/`HORIZONS` 模块常量。Task 5 的 `_sec_backfill` 与二期 ask.py/命中率统计复用。

- [ ] **Step 1: 写测试基建与失败测试**

`watch/tests/conftest.py`：

```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

`watch/tests/util.py`（合成行情 + 1x1 PNG，后续任务共用）：

```python
# -*- coding: utf-8 -*-
"""测试公用:合成 indicators parquet(现价口径可控)与 1x1 PNG。"""
import base64

import pandas as pd

PNG1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def write_px(path, px, hi_pct=1.02, lo_pct=0.98, start="2026-08-03"):
    """合成 indicators parquet:现价序列 px;factor 后半段翻倍(后复权价翻倍,现价不变),
    用于断言现价口径(close=px*factor,high/low 同放大)。返回 DatetimeIndex。"""
    n = len(px)
    idx = pd.bdate_range(start, periods=n)
    factor = [1.0] * (n // 2) + [2.0] * (n - n // 2)
    df = pd.DataFrame({
        "close": [p * f for p, f in zip(px, factor)],
        "high": [p * hi_pct * f for p, f in zip(px, factor)],
        "low": [p * lo_pct * f for p, f in zip(px, factor)],
        "factor": factor,
    }, index=idx)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return idx
```

`watch/tests/test_backfill.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/test_backfill.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'backfill'`

- [ ] **Step 3: 实现 watch/backfill.py**

```python
# -*- coding: utf-8 -*-
"""backfill.py —— 触发事件回填:archive 事件 join 最新行情 → 前向结局(研究口径)。
口径:一律现价(close/factor)从 indicators parquet 现算,事件表 close 仅作展示对照;
不读 mav/dist_ma 列,天然免疫新旧均线列名混存。fwd5/10/20 = 触发日后第N交易日收盘/
触发日收盘-1(不足N日 None);MFE/MAE = 触发次日至最新期间 high.max()/low.min() 相对
触发日收盘;elapsed=已过交易日数,恰为5/10/20 时 milestone 标记(HTML高亮"满N日")。
用法: python backfill.py        # 直接打印最近20交易日事件的验证表
"""
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
IND = os.path.join(os.path.dirname(HERE), "stockdata", "indicators")
ARCHIVE_F = os.path.join(HERE, "archive.parquet")
HORIZONS = (5, 10, 20)


def forward_stats(archive_f=ARCHIVE_F, ind_dir=IND, lookback=20, horizons=HORIZONS):
    """archive 最近 lookback 个交易日事件 → 前向结局 DataFrame(空表=无归档/无文件)。"""
    if not os.path.exists(archive_f):
        return pd.DataFrame()
    arc = pd.read_parquet(archive_f)
    if arc.empty:
        return pd.DataFrame()
    arc["date"] = pd.to_datetime(arc["date"])
    keep = sorted(arc["date"].unique())[-lookback:]
    rows = []
    for _, e in arc[arc["date"].isin(keep)].iterrows():
        p = os.path.join(ind_dir, "%s.parquet" % e["code"])
        if not os.path.exists(p):
            continue
        try:
            df = pd.read_parquet(p, columns=["close", "high", "low", "factor"])
            df.index = pd.DatetimeIndex(df.index)
            ti = df.index.get_indexer([e["date"]])[0]
            if ti < 0:                                        # 触发日不在该票行情里(数据残缺)
                continue
            f = df["factor"]
            px, hi, lo = df["close"] / f, df["high"] / f, df["low"] / f   # 现价口径
            base = float(px.iloc[ti])
            n = len(px) - 1 - ti                              # 已过交易日数
            row = {"code": e["code"], "name": e.get("name", ""), "rule": e["rule"],
                   "date": str(e["date"].date()), "trig_close": round(base, 2),
                   "elapsed": n, "ret_now": None, "mfe": None, "mae": None,
                   "milestone": n if n in horizons else None}
            for h in horizons:
                row["fwd%d" % h] = None if n < h else round(float(px.iloc[ti + h] / base - 1), 4)
            if n > 0:
                row["ret_now"] = round(float(px.iloc[-1] / base - 1), 4)
                row["mfe"] = round(float(hi.iloc[ti + 1:].max() / base - 1), 4)
                row["mae"] = round(float(lo.iloc[ti + 1:].min() / base - 1), 4)
            rows.append(row)
        except Exception:
            continue                                          # 单票数据异常不阻断整表
    cols = ["code", "name", "rule", "date", "trig_close", "elapsed",
            "ret_now", "fwd5", "fwd10", "fwd20", "mfe", "mae", "milestone"]
    return pd.DataFrame(rows, columns=cols).sort_values(["date", "code"]).reset_index(drop=True)


if __name__ == "__main__":
    st = forward_stats()
    if st.empty:
        print("(无归档事件)")
    else:
        with pd.option_context("display.width", 200, "display.max_columns", None):
            print(st.to_string(index=False))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/test_backfill.py -v`
Expected: 3 passed

- [ ] **Step 5: 真数据冒烟**

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python backfill.py | head -15`
Expected: 打印 2026-09-17/18/21 的事件行，elapsed≤3，fwd 列多为 None（数据还太新），无异常栈

- [ ] **Step 6: Commit**

```bash
git add watch/backfill.py watch/tests/
git commit -m "feat(watch): backfill前向收益回填(研究口径fwd5/10/20+MFE/MAE+milestone)"
```

---

### Task 2: scan.py 快报 JSON 落盘

**Files:**
- Modify: `watch/scan.py`（`llm_brief` 函数后新增 `save_briefs`；`__main__` 落盘 md 的代码块内加一行调用）
- Test: `watch/tests/test_scan.py`

**Interfaces:**
- Consumes: Task 1 无依赖；`events` 元素含 code/rule/close/thesis 键（scan.run 产出）
- Produces: `save_briefs(date, briefs, events, out_dir=None) -> str|None`，写 `{"date": …, "briefs": {code: {rules, close, thesis, brief}}}`；默认落 `watch/reports/briefs/<date>.json`。Task 5 `_load_brief` 与二期 ask.py 读此格式。

- [ ] **Step 1: 写失败测试**

`watch/tests/test_scan.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/test_scan.py -v`
Expected: FAIL，`ImportError: cannot import name 'save_briefs'`

- [ ] **Step 3: 实现**

`watch/scan.py` 中 `llm_brief`（约 297-315 行）之后新增：

```python
BRIEFS_DIR = os.path.join(HERE, "reports", "briefs")


def save_briefs(date, briefs, events, out_dir=None):
    """LLM 六段快报结构化落盘(层4命中率的物理前提);重复运行同日覆盖(幂等)。"""
    if not briefs:
        return None
    d = out_dir or BRIEFS_DIR
    os.makedirs(d, exist_ok=True)
    first, merged = {}, {}
    for e in events:
        first.setdefault(e["code"], e)
        merged[e["code"]] = (merged[e["code"]] + "+" + e["rule"]) if e["code"] in merged else e["rule"]
    payload = {"date": date, "briefs": {
        code: {"rules": merged.get(code, ""), "close": first[code]["close"],
               "thesis": first[code].get("thesis", ""), "brief": txt}
        for code, txt in briefs.items()}}
    p = os.path.join(d, "%s.json" % date)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return p
```

`__main__` 中落盘 md 的代码块（现 `with open(os.path.join(rd, events[0]["date"] + ".md"), …)` 一段）末尾追加一行：

```python
        save_briefs(events[0]["date"], briefs, events)
```

（Task 6 会重构 `__main__` 流程，此行届时并入。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/test_scan.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add watch/scan.py watch/tests/test_scan.py
git commit -m "feat(watch): LLM六段快报结构化落盘briefs/<date>.json"
```

---

### Task 3: report_html.py 骨架 + ①大盘 ②板块留位 ④跟踪 + index

**Files:**
- Create: `watch/report_html.py`
- Test: `watch/tests/test_report_html.py`

**Interfaces:**
- Consumes: `charts` dict（`{"market": png路径, code: png路径}`，scan `__main__` 产出）；`market` dict（`_market_scan()` 返回值：date/adv/dec/flat/above/total/med_vr/bench_d5/bench_d20/bench_dist250）；`tracking` list（`run()` 返回：code/name/rule/days）
- Produces: `build(events, tracking, briefs, charts, market, date, out_dir=REPORTS) -> str(html路径)`；`write_index(out_dir=REPORTS)`；`_b64(path)`；`_pct(x, nd=1)`；`_f(x)`；`_f2(x)`；`seg(brief, n=5)`（Task 4 实现，本任务先占位于 fmt 区之后——见 Step 3 注）。Task 4/5 在本文件追加 section 渲染器并挂进 `build` 的列表。

- [ ] **Step 1: 写失败测试**

`watch/tests/test_report_html.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/test_report_html.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'report_html'`

- [ ] **Step 3: 实现 watch/report_html.py（骨架版）**

```python
# -*- coding: utf-8 -*-
"""report_html.py —— 静态 HTML 日报(层2主展示形态):六区结构,单文件自包含。
图 base64 内嵌、CSS 内联、Jinja2 模板内嵌;一天一文件 + 滚动 index.html。
scan.py 末尾自动调用;独立重渲染: python report_html.py <date>(Task 5 加 main)。
区:①大盘总览 ②板块横览(留位) ③触发卡片 ④跟踪中 ⑤历史回填 ⑥追问记录。
"""
import base64
import glob
import os

import jinja2
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(HERE, "reports")
IND = os.path.join(os.path.dirname(HERE), "stockdata", "indicators")

CSS = """body{margin:0;font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:#fcfcfb;color:#0b0b0b}header{padding:16px 20px;border-bottom:1px solid #e1e0d9}
h1{font-size:20px;margin:0 0 8px}nav a{margin-right:14px;color:#2a78d6;text-decoration:none;font-size:13px}
main{max-width:960px;margin:0 auto;padding:12px 20px 40px}
section{margin:22px 0;padding:14px 16px;border:1px solid #e1e0d9;border-radius:8px;background:#fff}
h2{font-size:16px;margin:0 0 10px}h2 small{color:#898781;font-weight:400;margin-left:8px}
.kv{margin:4px 0}.muted{color:#898781}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:5px 8px;border-bottom:1px solid #e1e0d9;text-align:left}
th{color:#898781;font-weight:500}img{max-width:100%;height:auto;margin:8px 0;border:1px solid #e1e0d9;border-radius:6px}
details{margin:8px 0;padding:6px 10px;background:#fcfcfb;border:1px dashed #c3c2b7;border-radius:6px}
summary{cursor:pointer;color:#2a78d6;font-size:13px}pre{white-space:pre-wrap;margin:6px 0}
.card{margin:18px 0;padding:14px 16px;border:1px solid #e1e0d9;border-radius:8px;background:#fff}
.card h3{margin:0 0 6px;font-size:15px}.card .head{color:#898781;font-size:13px;margin-bottom:8px}
.pos{color:#d03b3b}.neg{color:#008300}tr.ms td{background:#fdf6e3}
.badge{display:inline-block;padding:0 6px;border-radius:8px;background:#2a78d6;color:#fff;font-size:11px;margin-left:6px}
footer{padding:14px 20px;color:#898781;font-size:12px;border-top:1px solid #e1e0d9}"""

PAGE_T = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>盯盘日报 {{ date }}</title><style>""" + CSS + """</style></head><body>
<header><h1>盯盘日报 {{ date }}</h1><nav>
<a href="#mkt">大盘</a><a href="#sector">板块</a><a href="#cards">触发</a>
<a href="#track">跟踪</a><a href="#backfill">回填</a><a href="#qa">追问</a>
<a href="index.html">索引</a></nav></header>
<main>{{ sections|safe }}</main>
<footer>生成于 {{ ts }} · stock_frame watch</footer></body></html>"""

MKT_T = """<section id="mkt"><h2>① 大盘总览 <small>{{ m.date }}</small></h2>
<p class="kv">涨{{ m.adv }} / 跌{{ m.dec }} / 平{{ m.flat }} ·
站上年线(MA250) {{ m.above }}/{{ m.total }} ({{ m.above_pct }}%) · 中位量比 {{ m.med_vr }}</p>
<p class="kv">沪深300 近5日 {{ m.d5 }} · 近20日 {{ m.d20 }} · 距年线 {{ m.dist250 }}</p>
{% if m.img %}<img src="{{ m.img }}" alt="大盘总览">{% else %}<p class="muted">大盘图未生成</p>{% endif %}
</section>"""

SECTOR_T = """<section id="sector"><h2>② 板块横览</h2>
<p class="muted">板块数据待接入(触发按行业分组、同板块多票=集群信号)</p></section>"""

TRACK_T = """<section id="track"><h2>④ 跟踪中 <small>持续触发未钝化</small></h2>
{% if rows %}<table><tr><th>代码</th><th>名称</th><th>规则</th><th>持续天数</th></tr>
{% for r in rows %}<tr><td>{{ r.code }}</td><td>{{ r.name }}</td><td>{{ r.rule }}</td><td>{{ r.days }}</td></tr>{% endfor %}
</table>{% else %}<p class="muted">无跟踪中条目</p>{% endif %}</section>"""


def _b64(path):
    """PNG → data URI;缺失返回 None(渲染降级为无图)。"""
    if not path or not os.path.exists(path):
        return None
    return "data:image/png;base64," + base64.b64encode(open(path, "rb").read()).decode()


def _nan(x):
    return x is None or (isinstance(x, float) and pd.isna(x))


def _pct(x, nd=1):
    """百分比;None/NaN → N/A。"""
    return "N/A" if _nan(x) else "%+.*f%%" % (nd, 100 * x)


def _f(x):
    return "N/A" if _nan(x) else "%g" % x


def _f2(x):
    return "N/A" if _nan(x) else "%.2f" % x


def _sec_market(**kw):
    st = kw["market"] or {}
    m = {"date": st.get("date", "?"), "adv": st.get("adv", "N/A"), "dec": st.get("dec", "N/A"),
         "flat": st.get("flat", "N/A"), "above": st.get("above", "N/A"), "total": st.get("total", "N/A"),
         "med_vr": _f2(st.get("med_vr")),
         "d5": _pct(st.get("bench_d5")), "d20": _pct(st.get("bench_d20")),
         "dist250": _pct(st.get("bench_dist250")),
         "above_pct": "%.0f" % (100.0 * st["above"] / st["total"]) if st.get("total") else "N/A",
         "img": _b64((kw["charts"] or {}).get("market"))}
    return jinja2.Template(MKT_T).render(m=m)


def _sec_sector(**kw):
    return SECTOR_T


def _sec_tracking(**kw):
    rows = kw["tracking"] or []
    return jinja2.Template(TRACK_T).render(rows=rows)


def build(events, tracking, briefs, charts, market, date, out_dir=REPORTS):
    """渲染单日 HTML + index.html,返回 HTML 路径;单区失败降级为错误行不阻断。"""
    os.makedirs(out_dir, exist_ok=True)
    secs = []
    for fn in (_sec_market, _sec_sector, _sec_tracking):
        try:
            secs.append(fn(events=events, tracking=tracking, briefs=briefs,
                           charts=charts, market=market, date=date, out_dir=out_dir))
        except Exception as ex:
            secs.append('<section><h2>⚠ %s 渲染失败: %s</h2></section>' % (fn.__name__, ex))
    html = jinja2.Template(PAGE_T).render(
        date=date, sections="\n".join(secs),
        ts=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"))
    p = os.path.join(out_dir, "%s.html" % date)
    with open(p, "w", encoding="utf-8") as f:
        f.write(html)
    write_index(out_dir)
    return p


def write_index(out_dir=REPORTS):
    """滚动索引:倒序链 reports/ 下所有 YYYY-MM-DD.html。"""
    pat = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].html"
    files = sorted(glob.glob(os.path.join(out_dir, pat)), reverse=True)
    rows = "".join('<tr><td>%s</td><td><a href="%s">打开</a></td></tr>'
                   % (os.path.basename(f)[:-5], os.path.basename(f)) for f in files)
    html = ('<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>盯盘日报索引</title>'
            '<style>body{font:14px/1.6 -apple-system,"PingFang SC",sans-serif;max-width:640px;margin:24px auto;'
            'padding:0 16px}table{border-collapse:collapse}td,th{padding:6px 12px;border-bottom:1px solid #e1e0d9}'
            'a{color:#2a78d6}</style></head><body><h1>盯盘日报索引</h1>'
            '<table><tr><th>日期</th><th></th></tr>%s</table></body></html>') % rows
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/test_report_html.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add watch/report_html.py watch/tests/test_report_html.py
git commit -m "feat(watch): HTML日报骨架(大盘/板块留位/跟踪区+滚动index)"
```

---

### Task 4: ③ 触发事件卡片（六段折叠/关键结论/对比 mini 表）

**Files:**
- Modify: `watch/report_html.py`（追加 `seg`/`_bench_row`/`_cmp_rows`/`_sec_cards`/`CARD_T`，挂进 `build`）
- Test: `watch/tests/test_report_html.py`（追加）

**Interfaces:**
- Consumes: `events` 元素（snapshot 全字段：close/dist_ma120/dist_ma250/dist_ma360/dd_52w/volr/rsi14/kdj/macd/boll/atr_pct/pos_52w/lo20/hi20/thesis）；`briefs` dict（code→六段文本）；Task 3 的 `_pct/_f/_f2/_b64`
- Produces: `seg(brief, n=5) -> str`（六段第 n 段提取，失败回退首行）——Task 5 回填区"LLM当时判定"列复用

- [ ] **Step 1: 追加失败测试**

`watch/tests/test_report_html.py` 追加：

```python
from report_html import build, seg


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
```

（`test_cards_render` 里 `MARKET` 复用文件顶部常量；`_bench_row` 需 close/volume/vma20/rsi_14 四列，`write_px` 只写行情四列，故此处手动建表。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/test_report_html.py -v`
Expected: 新增 3 个 FAIL（`ImportError: cannot import name 'seg'` 等），原 2 个仍 PASS

- [ ] **Step 3: 实现（追加到 report_html.py）**

在 `_f2` 之后追加：

```python
_CN = {1: "①", 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥"}


def seg(brief, n=5):
    """六段文本取第n段(①~⑥编号每段一行);定位失败回退首行(LLM格式跑偏不炸渲染)。"""
    if not brief:
        return ""
    for line in brief.splitlines():
        s = line.strip()
        if s.startswith(_CN[n]):
            return s[1:].strip() or s
    return brief.strip().splitlines()[0]
```

在 `SECTOR_T` 模板之后追加：

```python
CARD_T = """<section id="cards"><h2>③ 今日触发 <small>{{ n }} 只</small></h2>
{% if not cards %}<p class="muted">今日无新触发</p>{% endif %}
{% for c in cards %}<div class="card" id="{{ c.code }}">
<h3>{{ c.code }} {{ c.name }} <span class="badge">{{ c.rules }}</span></h3>
<p class="head">现价 {{ c.close }} · {{ c.ma_line }} · 距52周高 {{ c.dd }} · 量比 {{ c.volr }}{% if c.verdict %} · {{ c.verdict }}{% endif %}</p>
<table><tr><th>RSI14</th><th>KDJ K/D/J</th><th>MACD柱</th><th>BOLL下/上</th><th>ATR%</th><th>52周位置</th><th>近20日区间</th></tr>
<tr><td>{{ c.facts.rsi }}</td><td>{{ c.facts.kdj }}</td><td>{{ c.facts.hist }}</td><td>{{ c.facts.boll }}</td>
<td>{{ c.facts.atr }}</td><td>{{ c.facts.pos }}</td><td>{{ c.facts.rng }}</td></tr></table>
{% if c.img %}<img src="{{ c.img }}" alt="{{ c.code }}">{% else %}<p class="muted">图未生成</p>{% endif %}
<details><summary>LLM 六段快报</summary><pre>{{ c.brief }}</pre></details>
<table><tr><th>对比</th><th>RSI14</th><th>量比</th><th>距MA120</th></tr>
{% for r in c.cmp %}<tr><td>{{ r.name }}</td><td>{{ r.rsi }}</td><td>{{ r.volr }}</td><td>{{ r.d120 }}</td></tr>{% endfor %}</table>
{% if c.thesis %}<p class="kv">thesis: {{ c.thesis }}</p>{% endif %}
</div>{% endfor %}</section>"""
```

在 `_sec_sector` 与 `_sec_tracking` 之间追加：

```python
def _bench_row(ind_dir=IND):
    """沪深300 同口径三指标(RSI14/量比/距MA120)现算;失败返回 None(对比表降级)。"""
    try:
        df = pd.read_parquet(os.path.join(ind_dir, "sh000300.parquet"),
                             columns=["close", "volume", "vma20", "rsi_14"])
        c = df["close"].iloc[-1]
        m120 = df["close"].rolling(120, min_periods=120).mean().iloc[-1]
        return {"name": "沪深300", "rsi": _f2(df["rsi_14"].iloc[-1]),
                "volr": _f2(df["volume"].iloc[-1] / df["vma20"].iloc[-1]),
                "d120": _pct(None if pd.isna(m120) else float(c / m120 - 1))}
    except Exception:
        return None


def _cmp_rows(e, events, bench):
    """mini对比:该票 → 沪深300 → 同日其他触发票(板块指数无数据,以基准+同日触发替代)。"""
    rows = [{"name": "%s 本票" % e["code"], "rsi": _f2(e.get("rsi14")),
             "volr": _f2(e.get("volr")), "d120": _pct(e.get("dist_ma120"))}]
    if bench:
        rows.append(bench)
    for o in events:
        if o["code"] != e["code"]:
            rows.append({"name": "%s %s" % (o["code"], o.get("name", "")),
                         "rsi": _f2(o.get("rsi14")), "volr": _f2(o.get("volr")),
                         "d120": _pct(o.get("dist_ma120"))})
    return rows


def _sec_cards(**kw):
    events, briefs = kw["events"] or [], kw["briefs"] or {}
    charts = kw["charts"] or {}
    if not events:
        return jinja2.Template(CARD_T).render(n=0, cards=[])
    bench = _bench_row()
    merged, first, order = {}, {}, []                       # 同票合并:卡头一行,快照只出一遍
    for e in events:
        if e["code"] not in merged:
            order.append(e)
        merged[e["code"]] = (merged[e["code"]] + "+" + e["rule"]) if e["code"] in merged else e["rule"]
        first.setdefault(e["code"], e)
    cards = []
    for e in order:
        b = (briefs.get(e["code"]) or "").strip()
        k, d, j = e.get("kdj") or (None, None, None)
        dif, dea, hist = e.get("macd") or (None, None, None)
        up, mid, low = e.get("boll") or (None, None, None)
        verdict = seg(b, 5)
        cards.append({
            "code": e["code"], "name": e.get("name", ""), "rules": merged[e["code"]],
            "close": _f(e.get("close")),
            "ma_line": " ".join("MA%d %s" % (n, _pct(e.get("dist_ma%d" % n)))
                                for n in (120, 250, 360) if e.get("dist_ma%d" % n) is not None),
            "dd": _pct(e.get("dd_52w")), "volr": _f2(e.get("volr")),
            "verdict": ("判定:⑤" + verdict) if "⑤" in b else "",   # 无⑤段不伪造判定行
            "img": _b64(charts.get(e["code"])) or "",
            "facts": {"rsi": _f(e.get("rsi14")),
                      "kdj": "/".join(_f(x) for x in (k, d, j)),
                      "hist": _f(hist), "boll": "%s/%s" % (_f(low), _f(up)),
                      "atr": _f(e.get("atr_pct")), "pos": _f(e.get("pos_52w")),
                      "rng": "%s~%s" % (_f(e.get("lo20")), _f(e.get("hi20")))},
            "brief": b, "thesis": e.get("thesis", ""),
            "cmp": _cmp_rows(e, events, bench)})
    return jinja2.Template(CARD_T).render(n=len(cards), cards=cards)
```

`build()` 的 section 列表加入 `_sec_cards`（在 `_sec_sector` 之后）：

```python
    for fn in (_sec_market, _sec_sector, _sec_cards, _sec_tracking):
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/test_report_html.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add watch/report_html.py watch/tests/test_report_html.py
git commit -m "feat(watch): 触发事件卡片(⑤段关键结论/六段折叠/对比mini表)"
```

---

### Task 5: ⑤ 历史回填区 + ⑥ 追问区 + 独立重渲染 main

**Files:**
- Modify: `watch/report_html.py`（追加 `_load_brief`/`_cls`/`_sec_backfill`/`_sec_qa`/`BF_T`/`QA_T`、`build` 加 `archive_f`/`ind_dir` 透传、`__main__` 重渲染入口）
- Test: `watch/tests/test_report_html.py`（追加）

**Interfaces:**
- Consumes: Task 1 `forward_stats(archive_f, ind_dir, lookback)`；Task 2 briefs JSON 格式；Task 4 `seg(brief, 5)`；qa 文件格式 `{"date":…, "qa": [{"code","q","a","ts"}]}`（二期 ask.py 写入）
- Produces: `build(events, tracking, briefs, charts, market, date, out_dir=REPORTS, archive_f=None, ind_dir=None)`——后两参数测试注入用，默认用 backfill 模块常量。CLI：`python report_html.py [date]` 从 archive/briefs/charts 重建该日 HTML（二期 ask.py 答完调用它刷新⑥区）。

- [ ] **Step 1: 追加失败测试**

`watch/tests/test_report_html.py` 追加（文件顶部已 `import json`；需再 `import pandas as pd`）：

```python
def test_backfill_and_qa_sections(tmp_path):
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
```

`ret_now` 数值核对：触发日现价 `px[6]=10.6`，末日 `px[-1]=10.9`，`ret_now=10.9/10.6-1≈+2.8%`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/test_report_html.py -v`
Expected: 新增 2 个 FAIL（⑤⑥区内容缺失），原 5 个仍 PASS

- [ ] **Step 3: 实现（追加到 report_html.py）**

文件顶部 `import` 区补 `import json`、`import sys`。在 `CARD_T` 模板之后追加：

```python
BF_T = """<section id="backfill"><h2>⑤ 历史回填 <small>近{{ lookback }}个交易日触发的后续验证</small></h2>
{% if rows %}<table><tr><th>触发日</th><th>代码</th><th>名称</th><th>规则</th><th>触发价</th>
<th>已过</th><th>至今</th><th>fwd5</th><th>fwd10</th><th>fwd20</th><th>期间最高</th><th>期间最低</th><th>LLM当时判定</th></tr>
{% for r in rows %}<tr{% if r.ms %} class="ms"{% endif %}><td>{{ r.date }}</td><td>{{ r.code }}</td><td>{{ r.name }}</td>
<td>{{ r.rule }}</td><td>{{ r.trig }}</td><td>{{ r.elapsed }}日{% if r.ms %} <span class="badge">满{{ r.ms }}日</span>{% endif %}</td>
<td class="{{ r.cls_now }}">{{ r.ret_now }}</td><td>{{ r.f5 }}</td><td>{{ r.f10 }}</td><td>{{ r.f20 }}</td>
<td class="{{ r.cls_mfe }}">{{ r.mfe }}</td><td class="{{ r.cls_mae }}">{{ r.mae }}</td><td class="muted">{{ r.verdict }}</td></tr>{% endfor %}
</table>
{% if hidden %}<details><summary>其余 {{ hidden }} 条(未满里程碑)</summary><table>
<tr><th>触发日</th><th>代码</th><th>规则</th><th>已过</th><th>至今</th><th>fwd5</th><th>fwd10</th><th>fwd20</th></tr>
{% for r in hidden_rows %}<tr><td>{{ r.date }}</td><td>{{ r.code }}</td><td>{{ r.rule }}</td><td>{{ r.elapsed }}日</td>
<td>{{ r.ret_now }}</td><td>{{ r.f5 }}</td><td>{{ r.f10 }}</td><td>{{ r.f20 }}</td></tr>{% endfor %}</table></details>{% endif %}
{% else %}<p class="muted">暂无可回填事件</p>{% endif %}</section>"""

QA_T = """<section id="qa"><h2>⑥ 追问记录</h2>
{% if qas %}{% for q in qas %}<details><summary>{{ q.code }} · {{ q.q }}</summary>
<pre>{{ q.a }}</pre><p class="muted">{{ q.ts }}</p></details>{% endfor %}
{% else %}<p class="muted">今日无追问(二期 ask.py 写入 qa/<date>.json 后自动出现)</p>{% endif %}</section>"""
```

在 `seg` 之后追加：

```python
def _load_brief(date, out_dir):
    """读该日 briefs JSON → {code: {...}};缺文件/损坏返回 {}。"""
    try:
        return json.load(open(os.path.join(out_dir, "briefs", "%s.json" % date),
                              encoding="utf-8"))["briefs"]
    except Exception:
        return {}


def _cls(x):
    """涨红跌绿(A股惯例)class;None → 空。"""
    return "" if _nan(x) else ("pos" if x > 0 else "neg" if x < 0 else "")
```

在 `_sec_cards` 之后追加：

```python
def _sec_backfill(**kw):
    import backfill as bf
    af = kw.get("archive_f") or bf.ARCHIVE_F
    idir = kw.get("ind_dir") or bf.IND
    st = bf.forward_stats(archive_f=af, ind_dir=idir)
    if st.empty:
        return jinja2.Template(BF_T).render(rows=[], hidden=0, hidden_rows=[], lookback=20)
    briefs_by_date = {d: _load_brief(d, kw["out_dir"]) for d in sorted(set(st["date"]))}

    def _row(r):
        v = seg(briefs_by_date.get(r["date"], {}).get(r["code"], {}).get("brief", ""), 5)
        return {"date": r["date"], "code": r["code"], "name": r.get("name", ""),
                "rule": r["rule"], "trig": _f(r["trig_close"]),
                "elapsed": r["elapsed"], "ms": r["milestone"],
                "ret_now": _pct(r["ret_now"]), "cls_now": _cls(r["ret_now"]),
                "f5": _pct(r["fwd5"]), "f10": _pct(r["fwd10"]), "f20": _pct(r["fwd20"]),
                "mfe": _pct(r["mfe"]), "cls_mfe": _cls(r["mfe"]),
                "mae": _pct(r["mae"]), "cls_mae": _cls(r["mae"]),
                "verdict": (v[:30] + "…") if len(v) > 30 else v}

    allr = [_row(r) for _, r in st.iloc[::-1].iterrows()]        # 新在前
    ms_rows = [r for r in allr if r["ms"]]                       # milestone 默认展示,其余折叠
    rest = [r for r in allr if not r["ms"]]
    return jinja2.Template(BF_T).render(rows=ms_rows, hidden=len(rest),
                                         hidden_rows=rest, lookback=20)


def _sec_qa(**kw):
    try:
        qas = json.load(open(os.path.join(kw["out_dir"], "qa", "%s.json" % kw["date"]),
                             encoding="utf-8")).get("qa", [])
    except Exception:
        qas = []
    return jinja2.Template(QA_T).render(qas=qas)
```

`build()` 改签名并在 section 循环透传（`_sec_backfill`/`_sec_qa` 挂在 `_sec_tracking` 之后）：

```python
def build(events, tracking, briefs, charts, market, date, out_dir=REPORTS,
          archive_f=None, ind_dir=None):
    """渲染单日 HTML + index.html,返回 HTML 路径;单区失败降级为错误行不阻断。
    archive_f/ind_dir:⑤区回填数据源注入(测试/独立场景用,默认 backfill 模块常量)。"""
    os.makedirs(out_dir, exist_ok=True)
    secs = []
    for fn in (_sec_market, _sec_sector, _sec_cards, _sec_tracking, _sec_backfill, _sec_qa):
        try:
            secs.append(fn(events=events, tracking=tracking, briefs=briefs, charts=charts,
                           market=market, date=date, out_dir=out_dir,
                           archive_f=archive_f, ind_dir=ind_dir))
        except Exception as ex:
            secs.append('<section><h2>⚠ %s 渲染失败: %s</h2></section>' % (fn.__name__, ex))
```

（`_sec_market` 等既有渲染器签名是 `**kw`，多传参无影响；其余部分不变。）

文件末尾追加独立重渲染入口：

```python
if __name__ == "__main__":
    """独立重渲染: python report_html.py [date] —— 从 archive/briefs/charts/qa 重建该日 HTML。"""
    import backfill as bf
    d = sys.argv[1] if len(sys.argv) > 1 else str(pd.Timestamp.today().date())
    events = []
    if os.path.exists(bf.ARCHIVE_F):
        arc = pd.read_parquet(bf.ARCHIVE_F)
        events = arc[arc["date"].astype(str) == d].to_dict("records")
    briefs = {}
    try:
        briefs = _load_brief(d, REPORTS)
    except Exception:
        pass
    charts = {"market": os.path.join(REPORTS, "charts", d, "market.png")}
    for e in events:
        charts[e["code"]] = os.path.join(REPORTS, "charts", d, "%s.png" % e["code"])
    market = {}
    try:
        import scan as _scan
        market = _scan._market_scan()
    except Exception:
        pass
    print(build(events, [], briefs, charts, market, d))
```

- [ ] **Step 4: 跑全部测试确认通过**

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/ -v`
Expected: 9 passed（test_backfill 3 + test_scan 2 + test_report_html 4……实际数以新增测试数为准，全绿即可）

- [ ] **Step 5: Commit**

```bash
git add watch/report_html.py watch/tests/test_report_html.py
git commit -m "feat(watch): 回填区(milestone高亮/LLM当时判定)+追问区+独立重渲染CLI"
```

---

### Task 6: scan.py 集成 + SKILL 文档 + 端到端验证

**Files:**
- Modify: `watch/scan.py`（`__main__` 重构：数据日先行、大盘图无条件生成、调 `report_html.build`、报告文本附详情路径）
- Modify: `.claude/skills/stock-data-update/SKILL.md`（追加测试跑法一节）
- Test: 端到端手工验证（本任务无新单测——集成点靠 e2e；`--push` 分支语义不变）

**Interfaces:**
- Consumes: Task 3-5 `report_html.build(...)`；Task 2 `save_briefs(...)`；既有 `chart.market_chart`/`chart.stock_chart`、`_market_scan()`
- Produces: 每日 `watch/reports/<数据日>.html` + `index.html`（无事件也生成）；企微/md 报告末行 `📄 当日详情: watch/reports/<数据日>.html`

- [ ] **Step 1: 重构 `__main__`**

用下面版本整体替换 `watch/scan.py` 的 `if __name__ == "__main__":` 块（从 `if __name__` 到文件末尾）。改动点：① `_market_scan`/数据日/图表目录提前到配图之前（无事件也定数据日、也画大盘图）；② HTML 生成挪到报告文本拼装之前（详情行进企微与 md）；③ md 落盘与 `save_briefs` 并入；④ push 分支原样保留：

```python
if __name__ == "__main__":
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--llm", action="store_true", help="每票事实后直接追加 LLM 六段解读")
    a = ap.parse_args()
    events, tracking = run(a.push)
    briefs = llm_brief(events) if (a.llm and events) else {}
    # 大盘统计/数据日先行:无触发日也要出①④⑤区,数据日=事件日→基准指数日→今天
    st = _market_scan()
    date = events[0]["date"] if events else (
        st.get("date") if st.get("date") not in (None, "?") else str(pd.Timestamp.today().date()))
    charts = {}
    cdir = os.path.join(HERE, "reports", "charts", date)
    os.makedirs(cdir, exist_ok=True)
    if st.get("bench_df") is not None:                      # 大盘图无条件生成(①区配图)
        try:
            import chart as _chart
            charts["market"] = _chart.market_chart(
                st["bench_df"], st, os.path.join(cdir, "market.png"))
        except Exception as ex:
            print("⚠ 大盘图失败: %s" % ex)
    if events:                                              # 每票一张(单图失败不阻断)
        try:
            import chart as _chart
            merged, first = {}, {}
            for e in events:
                merged[e["code"]] = (merged[e["code"]] + "+" + e["rule"]) if e["code"] in merged else e["rule"]
                first.setdefault(e["code"], e)
            for code, e in first.items():
                try:
                    df = pd.read_parquet(os.path.join(IND, "%s.parquet" % code))
                    df.index = pd.DatetimeIndex(df.index)
                    charts[code] = _chart.stock_chart(
                        df, code, e["name"], merged[code], e["close"],
                        os.path.join(cdir, "%s.png" % code))
                except Exception as ex:
                    print("⚠ %s 配图失败: %s" % (code, ex))
        except Exception as ex:
            print("⚠ 个股配图失败: %s" % ex)
    # HTML 日报(层2主展示形态;无事件也生成,失败不阻断推送)
    # 覆盖保护:当日完整版已存在且本次无新事件(状态已消费的重复运行)则不覆盖——
    # 否则重复跑 scan 会把有卡片的日报覆盖成空事件版;完整重建用 python report_html.py <date>
    html_p = os.path.join(HERE, "reports", "%s.html" % date)
    html_path = None
    try:
        import report_html
        if events or not os.path.exists(html_p):
            html_path = report_html.build(events, tracking, briefs, charts, st, date)
        else:
            html_path = html_p
    except Exception as ex:
        print("⚠ HTML日报失败: %s" % ex)
    report_txt = format_report(events, tracking, briefs)
    if html_path:
        report_txt += "\n\n📄 当日详情: watch/reports/%s.html" % date
    print(report_txt)
    if a.llm and events:                                    # 落盘完整版md + 快报JSON
        rd = os.path.join(HERE, "reports")
        os.makedirs(rd, exist_ok=True)
        with open(os.path.join(rd, date + ".md"), "w", encoding="utf-8") as f:
            f.write(report_txt)
        save_briefs(date, briefs, events)
    if a.push and events:
        hook = os.environ.get("WATCH_WEBHOOK", "")
        sckey = os.environ.get("SERVERCHAN_KEY", "")
        if hook:
            import base64
            import hashlib
            import re
            import time as _t

            def _send(payload):
                urllib.request.urlopen(urllib.request.Request(
                    hook, data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"}), timeout=10)
                _t.sleep(3.1)                          # 企微机器人限 20 条/分钟

            def _img(p):
                b = open(p, "rb").read()
                return {"msgtype": "image",
                        "image": {"base64": base64.b64encode(b).decode(),
                                  "md5": hashlib.md5(b).hexdigest()}}

            n_img = 0
            blocks = [b.strip() for b in report_txt.split("\n\n") if b.strip()]
            for i, blk in enumerate(blocks):            # 企微 markdown 上限 4096 字节
                for j in range(0, len(blk), 3800):
                    _send({"msgtype": "markdown", "markdown": {"content": blk[j:j + 3800]}})
                if i == 0 and "market" in charts:      # 头部块后跟大盘总览图
                    _send(_img(charts["market"]))
                    n_img += 1
                m = re.match(r"\*\*(sh\d{6}|sz\d{6}|bj\d{6})", blk)
                if m and m.group(1) in charts:          # 各票文字块后跟该票图
                    _send(_img(charts[m.group(1)]))
                    n_img += 1
            print("已推送企微: %d 个文本块 + %d 张图" % (len(blocks), n_img))
        elif sckey:
            body = urllib.parse.urlencode({
                "title": "盘后监控:%d 触发(%s)" % (len(briefs), events[0]["date"]),
                "desp": report_txt[:30000]}).encode()
            urllib.request.urlopen(urllib.request.Request(
                "https://sctapi.ftqq.com/%s.send" % sckey, data=body), timeout=15)
            print("已推送 Server酱(含LLM解读)")
```

- [ ] **Step 2: 端到端验证（不推送）**

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python scan.py --llm`
Expected:
- 控制台打印快报（今日状态已被晨跑消费，大概率无新触发 → "无触发"或少量触发均可）
- 无新事件 + `2026-09-21.html` 不存在 → 首次生成（数据日回落到基准指数末日 09-21）；再跑一次 → 走覆盖保护，文件 mtime 不变
- `grep -c "data:image/png" watch/reports/2026-09-21.html` ≥ 1（大盘图已无条件内嵌）
- ⑤区可见 09-17/18/21 触发事件的 elapsed/至今 列（fwd 多为 N/A，数据尚新）
- `watch/reports/index.html` 存在且含 2026-09-21 链接
- 无新事件时：**不应**新增 briefs JSON（`ls watch/reports/briefs/` 与跑前一致）

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python report_html.py 2026-09-21`
Expected: 从 archive 重建含 09-21 全部触发卡片的完整版 HTML（覆盖空事件版），打印输出路径；打开无异常栈

Run: `cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/ -v`
Expected: 全部 passed

- [ ] **Step 3: 浏览器目检（人工）**

用浏览器打开 `watch/reports/index.html` → 点进当日日报，检查：六区齐全、锚点导航可跳、卡片折叠可展开、回填表 milestone 行底色、无外链资源（断网可开）。

- [ ] **Step 4: SKILL.md 追加测试跑法**

`.claude/skills/stock-data-update/SKILL.md` 末尾追加一节：

```markdown
## watch 单元测试

盯盘/backfill/日报渲染的单测（改动 watch/ 下代码后必跑）：

```bash
cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/ -v
```
```

- [ ] **Step 5: Commit**

```bash
git add watch/scan.py .claude/skills/stock-data-update/SKILL.md
git commit -m "feat(watch): scan集成HTML日报(无事件也生成/企微附详情路径)"
```

---

## 自检记录（写计划时已过）

- **Spec 覆盖**：文件布局(T1/T3)、scan三处改动(T2/T6)、数据日口径(T6)、backfill口径与CLI(T1)、六区①②③④⑤⑥(T3/T4/T5)、六段解析容错(T4)、index(T3)、测试策略(T1-T5)、SKILL跑法(T6)、e2e(T6) 均有对应任务
- **对 spec 的三处实现级修正**：⑤区默认只列 milestone 行+details折叠全量（323事件平铺不可读，已向用户说明）；大盘图从"仅有事件时生成"改为"无条件生成"（无事件日①区也要有图，spec"无触发日也生成"的自然推论）；scan 集成加**当日 HTML 覆盖保护**（重复运行不把完整日报覆盖成空事件版，完整重建走 `python report_html.py <date>`）
- **类型一致性**：`forward_stats` 列名在 T1 定义、T5 `_row` 按同名列读取；`seg` T4 定义 T5 复用；`build` 签名 T3 定义 T5 扩展（新增带默认值参数，向后兼容）
