# 盯盘四层展示 · 一期：HTML 日报壳 + 快报 JSON + 回填区

日期：2026-09-22
状态：已评审通过（用户确认设计，四项关键决策见下）

## 背景与总体架构

盯盘系统现有链路（`watch/scan.py`）：规则触发 → snapshot（29 字段）→ `archive.parquet` → GLM 六段快报 → `.md` 报告 + PNG 图 → 企微推送。此为"通知层"，只能响铃，缺详情/追问/验证回填三层。

目标架构（四层）：

| 层 | 载体 | 状态 |
|---|---|---|
| 1 通知 | 企微推送（摘要+图） | 已有，保留 |
| 2 详情 | 静态 HTML 日报（主展示形态） | **本期** |
| 3 交互 | ask.py 追问 CLI | 二期 |
| 4 复盘 | 自动回填（N 天前触发 → 今日结局） | **本期（展示端）** |

选静态 HTML 而非 Web 应用的理由：盘后批量生成、一次性阅读场景；单文件自包含（图 base64 内嵌）可归档可搜索；零维护零安全面。Streamlit/FastAPI 留待静态不够用时再议。

## 已确认决策

1. **分期**：两期。一期 = report_html.py（含⑤回填区+⑥追问区占位）+ LLM 快报 JSON 落盘 + backfill.py；二期 = ask.py。
2. **回填口径**：研究管线口径——至今涨跌幅 + fwd5/fwd10/fwd20 + 期间 MFE/MAE。
3. **快报存档**：每日 JSON `watch/reports/briefs/<数据日>.json`，与 archive.parquet（纯数值）职责分离。
4. **输出目录**：`watch/reports/`（与现有 .md/charts 同目录），非新建 out/。

## 文件布局

```
watch/
├─ scan.py          # 改（三处，最小侵入）
├─ backfill.py      # 新：archive 事件 → 前向收益（纯计算，可独立 CLI 跑）
├─ report_html.py   # 新：渲染日报 + index.html（Jinja2 模板内嵌，CSS 内联）
└─ reports/
    ├─ <数据日>.html
    ├─ index.html
    ├─ briefs/<数据日>.json
    └─ charts/<数据日>/*.png   # 已有，不改
```

backfill（分析）与 report_html（展示）分离：二期命中率统计直接复用 backfill。

## scan.py 改动

1. **快报落盘**：`llm_brief()` 返回后写 `reports/briefs/<数据日>.json`：
   ```json
   {"date": "2026-09-22", "briefs": {"sh600519": {"rules": "weekly_b1", "close": 1420.0, "thesis": "…", "brief": "①…\n②…\n…\n⑥…"}}}
   ```
   无 key/无事件不写。重复运行同日覆盖（幂等）。
2. **调用渲染**：图表生成后 `import report_html; report_html.build(events, tracking, briefs, charts, 市场统计)`。**无触发日也生成**（①④⑤区仍有内容）。
3. **企微末行**：报告文本末尾追加 `📄 当日详情: watch/reports/<数据日>.html`。

**数据日口径**：有事件取 `events[0]["date"]`；无事件取 `_market_scan()` 的数据日（基准指数末行日期）；再退回今天。

## backfill.py（新）

```python
def forward_stats(archive_f=ARCHIVE_F, ind_dir=IND, lookback=20, horizons=(5, 10, 20)) -> pd.DataFrame
```

- 取 archive 最近 `lookback` 个交易日的事件；每票读一次 parquet。
- **前向收益一律现价口径从 parquet 现算**（`close[i]/factor[i]`），事件表 `close` 仅作展示对照：
  - `fwd5/10/20` = 触发日后第 N 交易日收盘 / 触发日收盘 − 1（不足 N 日为 None）
  - `ret_now` = 最新收盘 / 触发日收盘 − 1
  - `mfe` / `mae` = 触发次日至最新期间 `high.max()` / `low.min()` 相对触发日收盘
  - `elapsed` = 已过交易日数（用于 5/10/20 整数天里程碑标记）
- 输出列：code/name/rule/触发日/触发价/elapsed/ret_now/fwd5/fwd10/fwd20/mfe/mae/milestone
- 兼容新旧列名（`mav60/240` 与 `mav250/360` 并存期）。
- CLI：`python backfill.py` 打印验证表（不依赖渲染）。

## report_html.py（新，六区结构）

Jinja2 模板内嵌为 Python 字符串（不落模板文件）；CSS 内联 `<style>`；配色延续 chart.py 浅色 ink/surface 体系；所有 PNG 转 base64 内嵌（单文件自包含）。

- **① 大盘总览**：宽度/涨跌家数/沪深300 距年线 + market.png（无图时仅数字）。
- **② 板块横览**：留位（"板块数据待接入"一行）。
- **③ 触发事件卡片 ×N**：
  - 卡片头一行：`票名 | 触发规则 | 现价 | 关键结论`（结论取六段第⑤段"与thesis对照"首句）
  - 事实块小表格（snapshot 字段）+ K线图 base64
  - `<details>` 折叠六段快报，默认收起
  - mini 对比表：**该票 vs 沪深300 vs 同日其他触发票** × RSI14/量比/距MA120。个股取事件 snapshot 字段；沪深300 从 `sh000300.parquet` 现算同口径
- **④ 跟踪中**：tracking 表格。
- **⑤ 历史回填 ★**：`backfill.forward_stats()` 表格；"LLM 当时判定"列取 `briefs/<触发日>.json` 该票第⑤段摘要；milestone 行高亮（"今日满10日"）。
- **⑥ 追问记录**：读 `reports/qa/<数据日>.json` 渲染（一期无此文件则空占位；二期 ask.py 写入后自动出现）。
- 页首锚点导航；每次 build 同步重生成 `index.html`（倒序表格链所有日期 html，相对路径）。

**六段解析容错**：按"每段一行、①~⑥编号"定位第⑤段；解析失败回退取首行。LLM 输出格式跑偏不影响渲染。

## 测试策略

新增 `watch/tests/`（pytest）：

- `test_backfill.py`：小型 fixture parquet（十几行合成数据），断言 fwd5/10/20/MFE/MAE 数值、不足期 None、milestone 标记。
- `test_report_html.py`：合成 events/tracking/briefs 渲染冒烟——断言含各区标题、图片 base64、`<details>` 结构、index.html 生成。

跑法补进 `.claude/skills/stock-data-update/SKILL.md`。

## 二期预告（本期不做）

- `ask.py`：`python ask.py sh600519 "缩量到什么程度算地量？"` → 读当日 briefs JSON + snapshot + 近10日序列 → GLM → 追加 `qa/<数据日>.json` → 重渲染该日 HTML（⑥区自动出现）。
- 命中率统计：briefs JSON 积累后 join `backfill` 前向结果出历史命中率报告。
