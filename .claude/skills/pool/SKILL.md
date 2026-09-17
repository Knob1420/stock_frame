---
name: pool
description: 观察池（observation pool）AI 对话层：研究今天的候选（读日报→联网反证→研究卡追加进日报§6→入池理由草稿）、入池 <codes>（candidates JSON 校验→snapshot 取数→写 pool.json）、复盘（只统计当前 cfg_version 已结案例→review 报告）、标注买入（只写 trade 字段）。只要用户提到观察池、池子、今天的候选、研究候选、入池/加进观察、复盘、理由兑现率、标注买入价格，或想看 pool/reports 日报与 pool.json 状态，都使用本 skill——即使用户没有说出"观察池"三个字。
---

# 观察池 AI 对话层

四句话驱动：**研究今天的候选 / 入池 \<codes\> / 复盘 / 标注买入 \<code\> \<price\>**。
分工：确定性管道（`pool/scan.py`）负责一切数字，本 skill 只做联网研究、理由起草、
以及经用户确认后的受控写入。口径与模块细节见 `pool/README.md`，冲突时以硬规则为准。

自然变体也认："看看今天的候选""研究下今天有什么票"→①；"603118 入池""把这两只加进观察池"→②；
"复盘一下""池子表现怎么样"→③；"603118 我 16.8 买了"→④。

## 文件地图

| 文件 | 内容 |
|---|---|
| `pool/reports/YYYY-MM-DD.md` | 日报六节；§6 AI 研究区是 AI 唯一可追加的地方 |
| `pool/reports/candidates-YYYYMMDD.json` | 当日候选机器可读源（入池校验唯一依据） |
| `pool/reports/review-YYYY-MM-DD.md` | 复盘报告（本 skill 产出） |
| `pool/pool.json`（+`.bak`） | 池状态机；写它只能经 `pool/pool.py`，损坏时 load 自动回退 .bak |
| `pool/lookup_tables/` | 18 桶胜率表（按 cfg_version 与桶方案版本 -b2 隔离） |

## 硬规则（动任何文件之前先读）

1. **不自己算指标。** 一切数字来自 candidates JSON、`scan.py snapshot` 输出或 pool.json 本身；
   不做"现价×量"之类任何自行推算，不手写桶胜率。
2. **AI 产出进 markdown，不进 JSON。** 研究卡与理由草稿只写日报 §6 或 review-*.md；
   `candidates-*.json` 永不改动。唯一例外：入池时经用户确认的 reason 文本与 reason_tags 写入 pool.json。
3. **入池校验以 candidates JSON 为唯一依据**（当日+上一交易日两份）。日报 md 的卡片列表、
   AI 记忆里"昨天聊过的票"都不算数。
4. **四句话之外不越权改池状态。** 不删条目、不改 outcome/max_up/min_dn/days、不代结案、
   不绕过 watching 去重；`pool.add_entry` 抛出的错要如实转述给用户，不要绕过。
5. 池模块禁 qlib；python 一律用 `.venv/bin/python`，工作目录=仓库根 `/home/admin/stock_selection`。
6. **不提供"批量一键入池"式快速确认**——逐只过理由，逐只确认。

reason_tags 受控词表（只用这六个，不自造）：`缩量回调 / J首拐 / 板块联动 / 大盘环境 / 技术形态 / 事件驱动`

---

## ① 研究今天的候选

目的是**反证**（找不入池的理由），不是找赞美。流程：

1. 定位最新日报：`pool/reports/` 下文件名最大的 `YYYY-MM-DD.md` 与对应 `candidates-YYYYMMDD.json`。
   若日报开头是"⚠ 数据滞后"→ 停下，让用户先跑数据更新（stock-data-update skill），不研究陈旧候选。
2. 读 §1 环境、§2 今日候选卡；默认取 candidates JSON 顺序（已按桶分排序）前 5 只，用户点名则以点名为准。
3. 逐只联网检索（WebSearch）：`<code> 利空`、`<code> 减持 OR 处罚 OR 问询 OR 预亏`、所属板块近期动态。
   本地无股票名源，检索以代码为主。
4. 把研究卡**追加**到日报 `## 6. AI 研究区` 小节之下（只追加，不动其他节；重复运行追加带日期新小节，不覆盖旧卡）：

   ```markdown
   ### <code>（MM-DD 研究卡）
   - 反证检索：<发现，或"未检出明显利空">（来源：<url>）
   - 板块/事件：<一句话>
   - 结论：通过 | 存疑 | 否决
   - 入池理由草稿：<一句话>；reason_tags：<受控词表子集>
   ```

5. 对话里给出推荐排序与理由草稿（供②使用）。桶胜率只是先验，结论措辞留有余地。

## ② 入池 <codes>（如"入池 603118 002536"）

对每个 code 依次执行；某只任一步不满足即拒该只并说明原因，不影响其余：

1. **校验**：取 `pool/reports/candidates-*.json` 文件名最大的两份（当日+上一交易日），code 需出现在
   任一份 `candidates[].sym`（sym 形如 `sh603118`/`sz002536`：6 开头前缀 sh，0 开头前缀 sz；
   用户若报带前缀的 sym，去前缀取 6 位代码再校验）。不在 → 拒绝，理由"只认当日/昨日候选"。
2. **理由确认**：向用户出示该只的理由草稿与 reason_tags（来自研究卡或现场起草，仍按反证口径），
   等用户确认或改写。逐只确认，不合并成一次批量确认。
3. **取数**（数字以此为准，--date=所命中那份候选文件的 date；当日候选可省略）：

   ```bash
   cd /home/admin/stock_selection
   .venv/bin/python pool/scan.py snapshot <code> --date <候选日> | tee /tmp/snap-<code>.json
   ```

4. **写入**（add_close/add_close_raw 取自候选 JSON；feats 取自 snapshot 输出）：

   ```bash
   cd /home/admin/stock_selection && .venv/bin/python - <<'EOF'
   import json, glob
   from pool import pool as P, cfg as C

   CODE, REASON, TAGS = "603118", "<用户确认后的理由>", ["缩量回调"]
   assert "<" not in CODE + REASON + "".join(TAGS), "占位符未替换——照抄模板会写入垃圾条目"
   assert len(CODE) == 6 and CODE.isdigit(), "CODE 需为 6 位数字代码"
   assert set(TAGS) <= {"缩量回调", "J首拐", "板块联动", "大盘环境", "技术形态", "事件驱动"}, "TAGS 越出受控词表"
   snap = json.load(open("/tmp/snap-%s.json" % CODE, encoding="utf-8"))
   files = sorted(glob.glob("pool/reports/candidates-*.json"))[-2:][::-1]   # 新份在前
   cjs = [json.load(open(f, encoding="utf-8")) for f in files]
   sym = ("sh" if CODE.startswith("6") else "sz") + CODE
   hit = next(((cj, c) for cj in cjs for c in cj["candidates"] if c["sym"] == sym), None)
   if hit is None:
       raise SystemExit("%s 不在当日/昨日候选，拒绝" % CODE)          # 正常应已在第1步拦截
   cj, cand = hit
   assert snap["date"] == cj["date"], "snapshot 日期 %s ≠ 候选日 %s（--date 漏传会把今日 feats 混进候选日基准）" % (snap["date"], cj["date"])
   state = P.load_pool(C.POOL_PATH)
   e = P.add_entry(state, CODE, "", REASON, TAGS,
                   {"env": cand["env"],
                    "j_low": snap["feats"]["j_low"], "dd": snap["feats"]["dd"],
                    "age": snap["feats"]["age"], "shrink": snap["feats"]["shrink"]},
                   cand["close"], cj["date"], [j["date"] for j in cjs], C.cfg_version(),
                   add_close_raw=cand["raw_close"])
   P.save_pool(state, C.POOL_PATH)
   print("已入池:", e["code"], e["added"], "基准(后复权) %.4f 现价 %.2f" % (e["add_close"], e["add_close_raw"]))
   EOF
   ```

   已在 watching 的重复入池会让 `add_entry` 抛 ValueError——如实转述，不要去重后静默跳过。

## ③ 复盘

只统计**当前公式版本**的已结案例（cfg_version 不同的旧条目绝不混入）：

```bash
cd /home/admin/stock_selection && .venv/bin/python - <<'EOF'
import json
from pool import pool as P, cfg as C
from pool.lookup import bucket_key

state = P.load_pool(C.POOL_PATH)
cur = C.cfg_version()
cases = [e for e in state["pool"] if e["cfg_version"] == cur and e.get("outcome")]
out = {"cfg_version": cur, "cases": len(cases),
       "watching": sum(1 for e in state["pool"] if e["status"] == "watching"),
       "stale_version": sum(1 for e in state["pool"] if e["cfg_version"] != cur),
       "by_type": {}, "by_bucket": {}, "by_tag": {}}
for e in cases:
    t = e["outcome"]["type"]
    out["by_type"][t] = out["by_type"].get(t, 0) + 1
    s = e["snapshot"]
    key = ("|".join(bucket_key(s["env"] == "牛性", s["j_low"], s["dd"]))
           if s.get("j_low") is not None and s.get("dd") is not None else "未知(特征缺失)")
    d = out["by_bucket"].setdefault(key, {"n": 0, "hit": 0})
    d["n"] += 1; d["hit"] += (t == "hit")
for tag in sorted({t for e in cases for t in e.get("reason_tags", [])}):
    sel = [e for e in cases if tag in e.get("reason_tags", [])]
    out["by_tag"][tag] = {"n": len(sel),
                          "hit_rate": round(sum(e["outcome"]["type"] == "hit" for e in sel) / len(sel), 3),
                          "mean_days": round(sum(e["outcome"]["days"] for e in sel) / len(sel), 1)}
print(json.dumps(out, ensure_ascii=False, indent=1))
EOF
```

然后写 `pool/reports/review-<今日日期>.md`：

- 标题注明 cfg_version；三个表：结案分布（hit/miss/flat 计数与占比）、理由兑现率（按 tag：案例数/hit率/平均历时）、桶分布（各桶 n 与 hit率）。
- 末尾如实注明 watching 只数与"排除旧公式版本条目 N 条"。
- **零案例时必须如实**：正文写"当前公式版本（<cfg_version>）无已结案例"，不得拿旧版本条目充数、
  不得编造分布；watching 只数与排除数照列，可另附"案例需入池后 10 个交易日起才有"的说明。

## ④ 标注买入 <code> <price>

只更新该条目的 trade 字段；price 为**现价口径**（用户实付价，非后复权）：

```bash
cd /home/admin/stock_selection && .venv/bin/python - <<'EOF'
from pool import pool as P, cfg as C
CODE, PRICE = "603118", 16.80
state = P.load_pool(C.POOL_PATH)
e = next((e for e in state["pool"] if e["code"] == CODE), None)
if e is None:
    raise SystemExit("%s 不在池中" % CODE)
P.annotate_trade(e, buy_price=PRICE)          # 用户给了日期可再加 buy_date="YYYY-MM-DD"
P.save_pool(state, C.POOL_PATH)
print("已标注:", CODE, e["trade"])
EOF
```

## 速查

- 单票取数：`.venv/bin/python pool/scan.py snapshot <code> [--date YYYY-MM-DD]`
  （JSON：close=后复权、raw_close=现价、chg、amount、env、feats、ma240/boll_mid/atr14/macd_hist）
- 池路径：`pool/pool.json`；当前版本：`pool.cfg.cfg_version()`；结案：hit +8% / miss −5% / 10 交易日 flat（T+1 起算，同日双触发按 miss）
- 已知限制与完整口径：`pool/README.md`
