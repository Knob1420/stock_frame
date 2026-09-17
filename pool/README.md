# 观察池（pool/）

J 指标触发的观察池 MVP：每日扫描候选 → 硬过滤+同型桶排序 → 日报 → 人工/AI 研究 →
入池跟踪 → 10 交易日内三判结案 → 复盘统计理由兑现率。日常对话入口见 skill
`.claude/skills/pool/SKILL.md`（研究今天的候选 / 入池 / 复盘 / 标注买入）。

## 模块地图

| 模块 | 职责 |
|---|---|
| `cfg.py` | 唯一配置点：路径常量、`CLOSE_CFG`（结案三判阈值）、`cfg_version()`（DEFAULT_CFG 内容 hash 指纹，公式换代自动隔离旧案例与旧 lookup） |
| `pool.py` | pool.json 状态机：`load_pool/save_pool`（.bak 备份 + tmp 原子替换 + 损坏回退，双损报错不清库）；`add_entry`（仅限当日/昨日候选 + watching 去重）；`track`（增量极值跟踪 + 三判结案，幂等可重放）；`annotate_trade`（现价口径买卖标注） |
| `lookup.py` | 同型桶：`feature_series`（j_low/dd/age/shrink）、`replay_outcome`（池口径全史回放）、`build_lookup/load_lookup`（18 桶 = 牛熊×J 3 档×回撤 3 档统计表，文件名带桶方案版本 `-b2`，换方案旧表不静默沿用） |
| `scan.py` | 每日确定性管道 `main_scan`：信号复用 `kdj.layers.signal`（零重算）→ 硬过滤 → 桶分排序 → 池内跟踪结案 → 日报六节 + candidates JSON；`snapshot <code>` 子命令供对话层取数 |
| `tests/` | pytest：IO/状态机/分桶/筛选/日报，共 57 例（含 stockdata 套件） |

## 口径

- **后复权判定 vs 现价显示**：信号、极值（max_up/min_dn）、结案、pool.json 存储全用后复权
  close（`add_close` 基准）；卡片与池内动态显示现价（`raw_close = close/factor`）。期间除权致
  两口径涨跌背离 >3pp 时日报加"（期间除权，比例按后复权）"注记，判定口径不变。
- **真实成交额 = parquet `amount` 列 × 1000**（列单位千元，源已按复权校准，勿再除 factor）。
  流动性下限 2 亿；另有 500 亿合理上限守卫拦源数据异常行。
- **涨停代理对称**：收盘涨幅 ≥9.7%（后复权 close/昨收）视为涨停——筛选剔除（次日追高不可操作）、
  桶回放跳过，两处口径对称。单测以不变式锁定。
- **结案三判**（`CLOSE_CFG`，池自有口径、与回测 E1/E3 无关）：T+1 起 10 个交易日内，
  最高价较基准严格越过 +8% → hit；最低价严格跌破 −5% → miss；同日双触发按 miss（保守）；
  到期无触发 → flat。"严格越过"含 1e-9 浮点容差，恰在阈值上不触发。
- **环境**：sh000300 close vs MA240 → 牛性/熊性（进桶键）。
- **版本隔离**：`cfg_version()` 变化 → lookup 表重开新档、复盘只统计新版本案例，旧案例绝不混入。

## 怎么跑

```bash
bash stockdata/daily_update.sh                                  # 0. 先更新数据（见 stock-data-update skill）
.venv/bin/python pool/scan.py                                   # 1. 主扫描：日报 + candidates JSON + 池跟踪
.venv/bin/python pool/scan.py snapshot 603118 [--date 2026-09-17]   # 2. 单票取数（JSON）
.venv/bin/python -m pytest pool/tests stockdata/tests -q         # 3. 全量测试
```

`daily_update.sh` 尾部已自动挂接主扫描（容错，不阻断主链）。对话层四句话用法见 skill。

## 产物

| 路径 | 内容 | git |
|---|---|---|
| `pool/reports/YYYY-MM-DD.md` | 日报六节；§6 AI 研究区由对话层追加 | 忽略 |
| `pool/reports/candidates-YYYYMMDD.json` | 当日候选机器可读源，入池校验唯一依据 | 忽略 |
| `pool/reports/review-YYYY-MM-DD.md` | 复盘报告（当前 cfg_version 已结案例） | 忽略 |
| `pool/pool.json` | 池状态；作为案例库备份层**有意入库**（仅 `.bak`/`.tmp` 被忽略） | 入库 |
| `pool/lookup_tables/lookup-<ver>-b2.json` | 18 桶统计 | 忽略 |

## 已知限制

- **ST 5% 涨停未被 9.7% 代理捕获**：ST 票收 5% 涨停即已"不可操作"，但仍可能进候选与桶统计。
- **sh000300 指数数据滞后一天**：环境标签慢一天（已知可接受，日报注明指数数据日）。
- **J 内三档区分度弱**：首跑分析显示熊市浅回撤桶中 J 越深胜率反略低——依据两处可查：
  设计文档 §15 待优化标记（仓库外 `/home/admin/stock/2026-09-16-observation-pool-mvp-design.md`）、
  首跑附检（gitignored `pool/reports/2026-09-17.md` 的"附:分桶有效性初检"一节）。
  J 内档信息量有限，留作后续观察/再分档依据。
- **本地无股票名源**：卡片只显示代码；AI 联网研究也以代码检索。
- **outcome.days 对停牌票可能少计**（n_elapsed 按市场日历占位，信息性字段）。
- **可能的后续开关**：批量一键入池（用户未批准，未实现）。

## 设计红线

- 池模块禁 qlib（唯一数据源 = `stockdata/indicators` parquet，路线 B）。
- AI 产出（研究卡/理由草稿）只进 markdown；入池校验只认 candidates JSON；
  一切数字来自管道（snapshot/candidates JSON），对话层不自算指标。
