# 观察池 MVP 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现观察池 MVP——每日 100 候选 → 脚本初筛 30 → 用户终审入池 → 自动跟踪结案 → 案例库复盘。

**Architecture:** 路线 B——`stockdata/indicators/` parquet（扩展原始价量+factor+amount 后共 26 列）是池的唯一数据源；`pool/` 三模块（pool.py 状态机 / lookup.py 同型桶统计 / scan.py 每日管道）只读 parquet，逐股流式处理，零 qlib API 依赖、零 OOM 风险；`layers.signal()` 原封不动复用。每日由 `daily_update.sh` 尾挂执行；AI 对话层经 `.claude/skills/pool/` skill 触发。

**Tech Stack:** Python 3.12（`.venv`：pandas 3.x / pyarrow / pytest 9）、qlib bin 数据（只读文本日历）、git。

**Spec:** `/home/admin/stock/2026-09-16-observation-pool-mvp-design.md`（含 2026-09-17 §0 修订）——本计划从该 spec 出发，执行者需同时读两份。

## Global Constraints

- 一切 Python 命令用 `/home/admin/stock_selection/.venv/bin/python`（系统 python 无依赖）
- 仓库根：`/home/admin/stock_selection`；pytest 从仓库根跑：`cd /home/admin/stock_selection && .venv/bin/python -m pytest pool/tests -q`
- 结案口径：hit `max_up ≥ +8%` / miss `min_dn ≤ −5%` / flat 窗口 10 交易日到期；T+1 起算；同日双触发按 miss；判定用后复权比例；trade 标注用现价
- parquet 口径：`open/high/low/close` 为**后复权**；`factor` 复权因子（**现价 = close/factor**）；`volume` 已被源数据按 1/factor 预调整（勿再除）；**真实成交额(元) = amount × 1000**（amount 即真实成交额千元——2026-09-17 Task 7 以茅台外部真值三重验证更正，旧口径 `÷factor×1000` 系双重校正，已废）
- 主板白名单：文件名匹配 `^(sh60|sz00)`（含 sz000/001/002/003；天然排除 sh00 指数族与 sz39 指数族）
- `cfg_version = "kdj-default-" + md5(json.dumps(DEFAULT_CFG, sort_keys=True))[:8]`；复盘/查表只认当前版本
- 池模块禁止 import qlib、禁止读 qlib_bin 二进制；唯一例外：读 `calendars/day.txt` 文本
- kdj / stockdata 现有测试零回归（每任务收尾必跑：`.venv/bin/python -m pytest stockdata/tests pool/tests -q`）
- git 提交信息末尾加：`Co-Authored-By: Claude Code <noreply@anthropic.com>`
- 次新/异常票单票跳过不炸整体（异常进日报"异常票"栏）
- 本地无股票名源（all.txt 无名称列）：候选/卡只显示代码；名称在入池时由对话层补充

---

### Task 0: git 仓库初始化

**Files:**
- Create: `/home/admin/stock_selection/.gitignore`

**Interfaces:**
- Produces: git 仓库（后续所有任务在此提交）

- [ ] **Step 1: 写 .gitignore**

```gitignore
.venv/
__pycache__/
.pytest_cache/
stockdata/indicators/
stockdata/logs/
pool/pool.json.bak
pool/pool.json.tmp
pool/reports/
pool/lookup_tables/
```

（`pool/pool.json` 是案例库，**跟踪**进 git 作为额外备份层；`.bak`/reports/lookup_tables 可再生，忽略。）

- [ ] **Step 2: 初始化并提交基线**

```bash
cd /home/admin/stock_selection
git init -b main
git add -A
git commit -m "chore: 初始化仓库——数据管道+kdj研究模块基线" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

- [ ] **Step 3: 验证**

Run: `git log --oneline && git status --short`
Expected: 一条提交；工作区干净（ignores 生效，indicators/ 未入库）

---

### Task 1: 指标库扩展原始价量 7 列

**Files:**
- Modify: `stockdata/indicators_conf.py`
- Modify: `stockdata/build_indicators.py`（FUNCS + run() 取数字段）
- Test: `stockdata/tests/test_indicators.py`

**Interfaces:**
- Produces: parquet 26 列 = 原 19 + `raw` 7 列（`open/high/low/close/volume/factor/amount`，后复权价+因子+真实成交额）；`sh000300.parquet` 已天然存在（all.txt 含指数），同样获得 26 列

- [ ] **Step 1: 写失败测试（追加到 test_indicators.py）**

```python
def test_raw_passthrough():
    n = 5
    g = pd.DataFrame({"$open": [1.0] * n, "$high": [2.0] * n, "$low": [0.5] * n,
                      "$close": [1.5] * n, "$volume": [100.0] * n,
                      "$factor": [1.0] * n, "$amount": [150.0] * n})
    out = bi.FUNCS["raw"](g)
    assert list(out.columns) == ["open", "high", "low", "close", "volume", "factor", "amount"]
    assert out["close"].tolist() == [1.5] * n
    assert out["factor"].tolist() == [1.0] * n
```

（注册表一致性测试 `test_registry_matches_funccs_output` 迭代 INDICATORS，"raw" 加入后自动覆盖。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/admin/stock_selection && .venv/bin/python -m pytest stockdata/tests/test_indicators.py -q`
Expected: FAIL（KeyError: 'raw' / 参数化键缺失）

- [ ] **Step 3: 实现——indicators_conf.py 的 INDICATORS 追加**

```python
    # 原始价量直通列(池/预警的单一数据源,路线B):open/high/low/close/volume 后复权,
    # factor 复权因子(现价=close/factor),amount 真实成交额(元)
    "raw":  {"params": {},
             "columns": ["open", "high", "low", "close", "volume", "factor", "amount"]},
```

- [ ] **Step 4: 实现——build_indicators.py 两处**

FUNCS 追加：

```python
    "raw":  lambda g: g[["$open", "$high", "$low", "$close", "$volume", "$factor", "$amount"]]
                   .rename(columns=lambda c: c.lstrip("$")),
```

run() 取数字段改为：

```python
    df = D.features(syms, ["$open", "$high", "$low", "$close", "$volume", "$factor", "$amount"],
                    disk_cache=0)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest stockdata/tests -q`
Expected: 全部 PASS（含原 20 项）

- [ ] **Step 6: 全量重算（~8 分钟，后台）**

```bash
cd /home/admin/stock_selection/stockdata && bash run_indicators_batch.sh
```

- [ ] **Step 7: 验证真实数据**

```bash
.venv/bin/python -c "
import pandas as pd
df = pd.read_parquet('stockdata/indicators/sh600000.parquet')
assert len(df.columns) == 26, df.columns
assert {'factor','amount','close'}.issubset(df.columns)
assert df['amount'].iloc[-1] > 1e7
sh = pd.read_parquet('stockdata/indicators/sh000300.parquet')
print('sh600000 26列 ✓ amount=%.0f万  sh000300 末日 %s' % (df['amount'].iloc[-1]/1e4, sh.index[-1]))
"
```
Expected: 26 列 ✓；sh000300 末日可能滞后一天（源数据特性，已知）

- [ ] **Step 8: 提交**

```bash
git add stockdata/indicators_conf.py stockdata/build_indicators.py stockdata/tests/test_indicators.py
git commit -m "feat: 指标库增加原始价量7列(raw)——池路线B单一数据源" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: pool 包骨架 + cfg 常量 + pool.json IO

**Files:**
- Create: `pool/__init__.py`（空）、`pool/cfg.py`、`pool/pool.py`
- Create: `pool/tests/conftest.py`、`pool/tests/test_pool_io.py`

**Interfaces:**
- Produces: `pool.cfg`：`PARQ_DIR/CALENDAR/REPORT_DIR/POOL_PATH/LOOKUP_DIR/CLOSE_CFG/cfg_version()`；`pool.pool`：`load_pool(path) -> dict`、`save_pool(pool, path)`、`PoolCorruptError`、`watching(pool) -> list`

- [ ] **Step 1: pool/cfg.py**

```python
# -*- coding: utf-8 -*-
"""pool/cfg.py —— 池的路径与口径常量(唯一配置点)。"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from kdj.layers import DEFAULT_CFG                     # noqa: E402(需先修 sys.path)

PARQ_DIR = os.path.join(ROOT, "stockdata", "indicators")
CALENDAR = "/home/admin/stockdata/qlib_bin/calendars/day.txt"
REPORT_DIR = os.path.join(ROOT, "pool", "reports")
POOL_PATH = os.path.join(ROOT, "pool", "pool.json")
LOOKUP_DIR = os.path.join(ROOT, "pool", "lookup_tables")

# 结案三判(spec §7):阈值同回测E3,窗口为池自己的10交易日口径
CLOSE_CFG = {"hit": 0.08, "miss": -0.05, "window": 10}


def cfg_version():
    """信号配置指纹:DEFAULT_CFG 内容 hash——公式换代自动隔离旧案例(spec §5)。"""
    h = hashlib.md5(json.dumps(DEFAULT_CFG, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
    return "kdj-default-" + h
```

- [ ] **Step 2: pool/tests/conftest.py**

```python
# -*- coding: utf-8 -*-
"""pool 测试公共:仓库根入 sys.path(kdj 包可导入)+ 合成 parquet 构造器。"""
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def make_df(n=600, seed=7, base=10.0):
    """合成 26 列子集 DataFrame(升序 bdate 索引),够 scan/lookup 用。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-02", periods=n).strftime("%Y-%m-%d")
    close = pd.Series(base + np.cumsum(rng.normal(0, 0.03, n)), index=idx)
    high = close * (1 + rng.uniform(0, 0.02, n))
    low = close * (1 - rng.uniform(0, 0.02, n))
    vol = pd.Series(rng.uniform(8e6, 3e7, n), index=idx)
    j = pd.Series(rng.uniform(0, 100, n), index=idx)
    df = pd.DataFrame({"open": close, "high": high, "low": low, "close": close,
                       "volume": vol, "factor": 1.0, "amount": close * vol,
                       "kdj_j": j, "ma240": base * 1.0, "boll_mid": base, "atr14": 0.5,
                       "macd_hist": 0.0})
    return df


def write_pq(path, df):
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_parquet(path)
```

- [ ] **Step 3: 写失败测试 pool/tests/test_pool_io.py**

```python
# -*- coding: utf-8 -*-
import json

import pytest

from pool import pool as P


def test_load_missing_returns_empty(tmp_path):
    p = tmp_path / "pool.json"
    assert P.load_pool(str(p)) == {"version": 1, "pool": []}


def test_save_creates_and_bak(tmp_path):
    p = str(tmp_path / "pool.json")
    d = {"version": 1, "pool": [{"code": "000651"}]}
    P.save_pool(d, p)
    assert P.load_pool(p) == d                      # 无 .bak 时首次写
    d["pool"].append({"code": "600941"})
    P.save_pool(d, p)
    assert json.load(open(p + ".bak"))["pool"] == [{"code": "000651"}]   # 备份的是上一版


def test_load_corrupt_falls_back_to_bak(tmp_path):
    p = tmp_path / "pool.json"
    P.save_pool({"version": 1, "pool": [{"code": "000651"}]}, str(p))
    open(p, "w").write("{broken!!")
    assert P.load_pool(str(p))["pool"] == [{"code": "000651"}]


def test_load_both_corrupt_raises(tmp_path):
    p = tmp_path / "pool.json"
    open(p, "w").write("{bad")
    open(p + ".bak", "w").write("{bad too")
    with pytest.raises(P.PoolCorruptError):
        P.load_pool(str(p))
```

- [ ] **Step 4: 跑测试确认失败**

Run: `cd /home/admin/stock_selection && .venv/bin/python -m pytest pool/tests/test_pool_io.py -q`
Expected: FAIL（ModuleNotFoundError: pool.pool）

- [ ] **Step 5: 实现 pool/pool.py（IO 部分）**

```python
# -*- coding: utf-8 -*-
"""pool.py —— pool.json 状态机:IO(备份/恢复)、入池校验、跟踪三判结案、标注(spec §5/§7)。"""
import json
import os
import shutil


class PoolCorruptError(RuntimeError):
    """pool.json 与 .bak 均损坏——停机报错,绝不静默清空案例库。"""


def load_pool(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"version": 1, "pool": []}
    except json.JSONDecodeError:
        bak = path + ".bak"
        if os.path.isfile(bak):
            with open(bak, encoding="utf-8") as f:
                return json.load(f)              # bak 也坏则此处抛 JSONDecodeError
        raise PoolCorruptError("pool.json 损坏且无 .bak 可恢复")


def save_pool(pool, path):
    if os.path.isfile(path):
        shutil.copy2(path, path + ".bak")        # 备份上一版
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)                        # 原子替换


def watching(pool):
    return [e for e in pool["pool"] if e["status"] == "watching"]
```

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/bin/python -m pytest pool/tests/test_pool_io.py -q`
Expected: 4 PASS

- [ ] **Step 7: 提交**

```bash
git add pool/__init__.py pool/cfg.py pool/pool.py pool/tests/
git commit -m "feat: pool包骨架+cfg常量+pool.json IO(备份/恢复)" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: pool.py 状态机（入池校验/跟踪结案/标注）

**Files:**
- Modify: `pool/pool.py`
- Test: `pool/tests/test_pool_state.py`

**Interfaces:**
- Consumes: Task 2 的 `load_pool/save_pool/watching`、`pool.cfg.CLOSE_CFG`
- Produces: `add_entry(pool, code, name, reason, reason_tags, snapshot, add_close, added, cand_dates, cfg_ver) -> entry`；`track(entry, bars, n_elapsed, cfg) -> outcome|None`（bars=`[(date, high, low, close)]` 后复权，n_elapsed=T+1 起至今交易日数按市场日历，停牌照占）；`annotate_trade(entry, **kw)`；entry 增维护字段 `days`（已过交易日计数，信息性）

- [ ] **Step 1: 写失败测试 pool/tests/test_pool_state.py**

```python
# -*- coding: utf-8 -*-
import pytest

from pool import pool as P
from pool.cfg import CLOSE_CFG, cfg_version

D = ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19", "2026-09-22",
     "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-29",
     "2026-09-30", "2026-10-08", "2026-10-09"]


def mk_entry(added=D[0]):
    return {"code": "000651", "name": "", "added": added, "cfg_version": cfg_version(),
            "add_close": 10.0, "reason": "", "reason_tags": [],
            "snapshot": {"env": "牛性", "j_low": 8, "drawdown": -0.18, "age": 23},
            "status": "watching", "max_up": 0.0, "min_dn": 0.0, "days": 0,
            "last_date": None, "last_close": None, "outcome": None, "trade": None}


def bar(d, hi, lo, c):
    return (d, hi, lo, c)


# ---------- 入池 ----------
def test_add_entry_ok_and_rejects():
    pool = {"version": 1, "pool": []}
    e = P.add_entry(pool, "000651", "格力电器", "缩量+J首拐", ["缩量回调", "J首拐"],
                    {"env": "牛性"}, 38.2, D[1], [D[0], D[1]], cfg_version())
    assert e["status"] == "watching" and pool["pool"][-1] is e
    with pytest.raises(ValueError):        # 隔日名单拒绝(快照失真)
        P.add_entry(pool, "600941", "", "", [], {}, 10, D[1], [D[0]], cfg_version())
    with pytest.raises(ValueError):        # watching 重复入池拒绝
        P.add_entry(pool, "000651", "", "", [], {}, 38, D[1], [D[1]], cfg_version())


def test_closed_can_reenter():
    pool = {"version": 1, "pool": []}
    P.add_entry(pool, "000651", "", "", [], {}, 10, D[0], [D[0]], cfg_version())
    pool["pool"][0]["status"] = "closed"
    P.add_entry(pool, "000651", "", "", [], {}, 11, D[1], [D[1]], cfg_version())  # 不抛
    assert len(pool["pool"]) == 2


# ---------- 跟踪与结案 ----------
def test_track_updates_extremes_idempotent():
    e = mk_entry()
    bars = [bar(D[1], 10.4, 9.9, 10.2), bar(D[2], 10.1, 9.5, 9.6)]
    assert P.track(e, bars, n_elapsed=2) is None          # 未触发
    assert e["max_up"] == pytest.approx(0.04) and e["min_dn"] == pytest.approx(-0.05 - 1e-9)
    P.track(e, bars, n_elapsed=2)                          # 同批重跑 → 幂等
    assert e["max_up"] == pytest.approx(0.04) and e["days"] == 2


def test_close_hit():
    e = mk_entry()
    o = P.track(e, [bar(D[1], 10.9, 9.9, 10.8)], n_elapsed=1)
    assert o["type"] == "hit" and e["status"] == "closed"
    assert P.track(e, [bar(D[2], 11.5, 9.0, 9.1)], n_elapsed=2) is None   # 结案后冻结


def test_close_miss():
    e = mk_entry()
    o = P.track(e, [bar(D[1], 10.1, 9.4, 9.5)], n_elapsed=1)
    assert o["type"] == "miss" and e["status"] == "closed"


def test_close_flat_on_window_expiry():
    e = mk_entry()
    bars = [bar(D[i + 1], 10.05, 9.96, 10.0) for i in range(CLOSE_CFG["window"])]
    o = P.track(e, bars, n_elapsed=CLOSE_CFG["window"])
    assert o["type"] == "flat" and o["days"] == CLOSE_CFG["window"]


def test_same_day_double_trigger_is_miss():
    e = mk_entry()
    o = P.track(e, [bar(D[1], 10.9, 9.4, 10.0)], n_elapsed=1)   # 同日 +9% 与 −6%
    assert o["type"] == "miss"


def test_hit_first_day_miss_later_stays_hit():
    e = mk_entry()
    o = P.track(e, [bar(D[1], 10.9, 9.9, 10.8)], n_elapsed=1)   # 第1日 hit → 立即结案
    assert o["type"] == "hit"


# ---------- 标注 ----------
def test_annotate_trade():
    e = mk_entry()
    P.annotate_trade(e, buy_date=D[1], buy_price=39.2)
    P.annotate_trade(e, sell_date=D[5], sell_price=41.0)
    assert e["trade"] == {"buy_date": D[1], "buy_price": 39.2, "sell_date": D[5], "sell_price": 41.0}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest pool/tests/test_pool_state.py -q`
Expected: FAIL（AttributeError: add_entry）

- [ ] **Step 3: 实现——pool.py 追加**

```python
# (接 IO 部分) —— 状态机
def add_entry(pool, code, name, reason, reason_tags, snapshot, add_close,
              added, cand_dates, cfg_ver):
    """入池:仅限当日/前一交易日候选;watching 去重(spec §9)。"""
    for e in watching(pool):
        if e["code"] == code:
            raise ValueError("%s 已在 watching( %s 入池),拒绝重复" % (code, e["added"]))
    if added not in cand_dates:
        raise ValueError("%s 不在候选名单日期 %s 中(只认当日/昨日)" % (code, cand_dates))
    entry = {"code": code, "name": name, "added": added, "cfg_version": cfg_ver,
             "add_close": add_close, "reason": reason, "reason_tags": reason_tags,
             "snapshot": snapshot, "status": "watching",
             "max_up": 0.0, "min_dn": 0.0, "days": 0,
             "last_date": None, "last_close": None, "outcome": None, "trade": None}
    pool["pool"].append(entry)
    return entry


def _trigger(entry, cfg):
    """当前极值是否触发(每根 bar 后调用;同日双触发按 miss 保守)。"""
    if entry["max_up"] >= cfg["hit"] and entry["min_dn"] <= cfg["miss"]:
        return "miss"
    if entry["max_up"] >= cfg["hit"]:
        return "hit"
    if entry["min_dn"] <= cfg["miss"]:
        return "miss"
    return None


def track(entry, bars, n_elapsed, cfg=CLOSE_CFG):
    """增量跟踪:bars 为 >last_date 的新K线(后复权);n_elapsed 为 T+1 起至今的
    市场交易日数(停牌照占,调用方从日历算)。触发即结案并返回 outcome;已结案返回 None。"""
    if entry.get("outcome") is not None:
        return None                                          # 冻结
    base = entry["add_close"]
    for d, hi, lo, c in bars:
        if d <= entry["added"] or (entry["last_date"] and d <= entry["last_date"]):
            continue                                         # 幂等:跳过已处理
        entry["max_up"] = max(entry["max_up"], hi / base - 1)
        entry["min_dn"] = min(entry["min_dn"], lo / base - 1)
        entry["last_date"], entry["last_close"] = d, c
        entry["days"] += 1
        t = _trigger(entry, cfg)
        if t:
            entry["outcome"] = {"type": t, "days": entry["days"],
                                "max_up": entry["max_up"], "min_dn": entry["min_dn"]}
            entry["status"] = "closed"
            return entry["outcome"]
    if n_elapsed >= cfg["window"]:                           # 窗口到期无触发
        entry["outcome"] = {"type": "flat", "days": n_elapsed,
                            "max_up": entry["max_up"], "min_dn": entry["min_dn"]}
        entry["status"] = "closed"
        return entry["outcome"]
    return None


def annotate_trade(entry, **kw):
    """可选买卖标注(现价口径,用户口头;None 值忽略)。"""
    entry["trade"] = {**(entry.get("trade") or {}), **{k: v for k, v in kw.items() if v is not None}}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest pool/tests -q`
Expected: 全 PASS（含 Task 2 的 IO 测试）

- [ ] **Step 5: 提交**

```bash
git add pool/pool.py pool/tests/test_pool_state.py
git commit -m "feat: pool状态机——入池校验/三判结案/幂等跟踪/买卖标注" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: lookup.py 同型桶统计

**Files:**
- Create: `pool/lookup.py`
- Test: `pool/tests/test_lookup.py`

**Interfaces:**
- Consumes: `kdj.layers`（DEFAULT_CFG/signal/bars_since/cross_up/ema2/long_ma）、`pool.cfg`（PARQ_DIR/LOOKUP_DIR/cfg_version）、`pool.pool.CLOSE_CFG`
- Produces:
  - `feature_series(df) -> dict[str, pd.Series]`（键 `j_low/dd/age/shrink`；df 需列 `j/high/close/volume`）
  - `signal_features(df, i=-1) -> dict`（同上取第 i 行,float/None）
  - `bucket_key(env_bull, j_low, dd) -> ("bull"|"bear", "j<=10"|"10<j<=15", "dd>-15"|"dd<=-15")`（超界钳制到边缘桶）
  - `replay_outcome(df, i, cfg) -> dict|None`（池口径结局；窗口数据不足返回 None）
  - `build_lookup(parq_dir, out_dir, cfg_ver) -> dict`；`lookup_path(ver) -> str`；`load_lookup(ver) -> dict|None`
  - lookup JSON：`{"cfg_version": v, "buckets": {"bull|j<=10|dd>-15": {"n", "win_rate", "payoff", "types"}}}`，win_rate=hit/全部结局（含 flat），payoff=hit组平均max_up÷|miss组平均min_dn|

- [ ] **Step 1: 写失败测试 pool/tests/test_lookup.py**

```python
# -*- coding: utf-8 -*-
import json

import pandas as pd
import pytest

from pool import lookup as K
from pool.cfg import cfg_version
from pool.tests.conftest import make_df


def test_bucket_key_and_clamp():
    assert K.bucket_key(True, 8.0, -0.10) == ("bull", "j<=10", "dd>-15")
    assert K.bucket_key(False, 13.0, -0.20) == ("bear", "10<j<=15", "dd<=-15")
    assert K.bucket_key(True, -30.0, -0.60) == ("bull", "j<=10", "dd<=-15")   # 超界钳制到边缘桶
    assert K.bucket_key(False, 40.0, 0.05) == ("bear", "10<j<=15", "dd>-15")  # 超上界同钳制


def test_replay_outcome_paths():
    n = 30
    idx = pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d")
    base = {"open": [10.0] * n, "high": [10.0] * n, "low": [10.0] * n, "close": [10.0] * n,
            "volume": [1e7] * n, "j": [50.0] * n}
    df = pd.DataFrame(base, index=idx)
    df.iloc[11, df.columns.get_loc("high")] = 10.9                       # T+1 触 +9% → hit
    assert K.replay_outcome(df, 10)["type"] == "hit"
    df2 = df.copy(); df2.iloc[11, df2.columns.get_loc("low")] = 9.4      # T+1 触 −6% → miss
    assert K.replay_outcome(df2, 10)["type"] == "miss"
    df3 = df.copy()                                                      # 全程 ±0.5% → flat
    for i in range(11, 21):
        df3.iloc[i, df3.columns.get_loc("high")] = 10.05
        df3.iloc[i, df3.columns.get_loc("low")] = 9.96
    assert K.replay_outcome(df3, 10)["type"] == "flat"
    assert K.replay_outcome(df3, 29) is None                             # 尾部窗口不足


def test_replay_same_day_double_is_miss():
    n = 30
    idx = pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d")
    df = pd.DataFrame({"open": [10.0] * n, "high": [10.0] * n, "low": [10.0] * n,
                       "close": [10.0] * n, "volume": [1e7] * n, "j": [50.0] * n}, index=idx)
    df.iloc[11, df.columns.get_loc("high")] = 10.9
    df.iloc[11, df.columns.get_loc("low")] = 9.4
    assert K.replay_outcome(df, 10)["type"] == "miss"


def test_build_lookup_versioned(tmp_path, monkeypatch):
    # 合成 2 只股票 + 1 只指数;signal 打桩为"第 10 行与倒数第 11 行为 True"
    import numpy as np
    import kdj.layers as L
    parq = tmp_path / "ind"; parq.mkdir()
    sh = make_df(n=600); sh["close"] = sh["close"] * 0 + 100.0           # 指数横盘
    sh.to_parquet(parq / "sh000300.parquet")
    for sym in ("sh600001", "sz000002"):
        make_df(seed=hash(sym) % 1000).to_parquet(parq / ("%s.parquet" % sym))

    def fake_signal(df, cfg):
        s = pd.Series(False, index=df.index)
        s.iloc[10] = True; s.iloc[-11] = True
        return s
    monkeypatch.setattr(L, "signal", fake_signal)
    out = K.build_lookup(parq_dir=str(parq), out_dir=str(tmp_path), cfg_ver="kdj-default-TEST")
    p = tmp_path / "lookup-kdj-default-TEST.json"
    assert p.is_file()
    data = json.load(open(p))
    assert data["cfg_version"] == "kdj-default-TEST"
    total = sum(b["n"] for b in data["buckets"].values())
    assert total == 4                                                    # 2股×2信号
    for b in data["buckets"].values():
        assert 0.0 <= b["win_rate"] <= 1.0
        assert set(b["types"]) <= {"hit", "miss", "flat"}
    # 换版本重生成 → 不同文件,旧文件保留(隔离)
    K.build_lookup(parq_dir=str(parq), out_dir=str(tmp_path), cfg_ver="kdj-default-V2")
    assert (tmp_path / "lookup-kdj-default-V2.json").is_file() and p.is_file()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest pool/tests/test_lookup.py -q`
Expected: FAIL（ModuleNotFoundError: pool.lookup）

- [ ] **Step 3: 实现 pool/lookup.py**

```python
# -*- coding: utf-8 -*-
"""lookup.py —— 同型桶:特征提取、全史回放、8桶统计表生成与查表(spec §6)。
判定口径=池自己的结案规则(hit+8%/miss−5%/flat 10日,T+1起算),与回测 E1/E3 无关。"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

from pool import cfg as C
from pool.pool import CLOSE_CFG

J_THR = 15.0          # 桶外边界=cfg L2 的 J 阈值(l2_low 默认 15,硬编码并注释)
DD_FLOOR_KEY = "depth"


def feature_series(df):
    """df 需列 j/high/close/volume。返回特征 Series(dict):j_low/dd/age/shrink。"""
    import kdj.layers as L
    v = df["volume"]
    return {
        "j_low": df["j"].rolling(5, min_periods=1).min(),
        "dd": df["close"] / df["high"].rolling(60, min_periods=1).max() - 1,
        "age": L.bars_since(L.cross_up(L.ema2(df["close"]), L.long_ma(df["close"]))),
        "shrink": (v.rolling(3, min_periods=3).mean().rolling(5, min_periods=1).min()
                   / v.rolling(20, min_periods=20).mean()),
    }


def signal_features(df, i=-1):
    fs = feature_series(df)
    out = {}
    for k, s in fs.items():
        v = s.iloc[i]
        out[k] = float(v) if v == v else None                            # NaN→None
    return out


def bucket_key(env_bull, j_low, dd):
    """8桶键;特征超界钳制到边缘桶(防换cfg后漏桶)。内部分档 10/-15% 为固定常数。"""
    from kdj.layers import DEFAULT_CFG
    je = min(j_low, J_THR)
    de = min(max(dd, DEFAULT_CFG[DD_FLOOR_KEY]), 0.0)
    return ("bull" if env_bull else "bear",
            "j<=10" if je <= 10 else "10<j<=15",
            "dd>-15" if de > -0.15 else "dd<=-15")


def replay_outcome(df, i, cfg=CLOSE_CFG):
    """行 i 信号日的池口径结局。基准=信号日 close;窗口=i+1..i+10 行。数据不足→None。"""
    base = df["close"].iloc[i]
    hu = (df["high"].iloc[i + 1: i + 1 + cfg["window"]] / base - 1).reset_index(drop=True)
    ld = (df["low"].iloc[i + 1: i + 1 + cfg["window"]] / base - 1).reset_index(drop=True)
    if len(hu) < cfg["window"]:
        return None
    hpos = int(np.argmax(hu.ge(cfg["hit"]).values)) if hu.ge(cfg["hit"]).any() else None
    mpos = int(np.argmax(ld.le(cfg["miss"]).values)) if ld.le(cfg["miss"]).any() else None
    if hpos is not None and mpos is not None:
        t, pos = ("miss", mpos) if mpos <= hpos else ("hit", hpos)      # 同日双触发→miss
    elif hpos is not None:
        t, pos = "hit", hpos
    elif mpos is not None:
        t, pos = "miss", mpos
    else:
        t, pos = "flat", cfg["window"] - 1
    return {"type": t, "days": pos + 1, "max_up": float(hu.max()), "min_dn": float(ld.min())}


def lookup_path(ver, out_dir=None):
    return os.path.join(out_dir or C.LOOKUP_DIR, "lookup-%s.json" % ver)


def build_lookup(parq_dir=None, out_dir=None, cfg_ver=None):
    """逐股流式回放全史信号→8桶统计(离线、一次性慢)。env=sh000300 close vs ma240。"""
    import kdj.layers as L
    from kdj.layers import DEFAULT_CFG
    parq_dir = parq_dir or C.PARQ_DIR
    out_dir = out_dir or C.LOOKUP_DIR
    cfg_ver = cfg_ver or C.cfg_version()
    os.makedirs(out_dir, exist_ok=True)
    sh = pd.read_parquet(os.path.join(parq_dir, "sh000300.parquet"))
    sh_bull = (sh["close"] > sh["ma240"])

    stat = {}
    for fp in sorted(glob.glob(os.path.join(parq_dir, "*.parquet"))):
        sym = os.path.basename(fp)[:-8]
        if not (sym.startswith("sh60") or sym.startswith("sz00")):
            continue                                   # 主板白名单
        df = pd.read_parquet(fp)
        if not {"high", "close", "volume", "kdj_j"}.issubset(df.columns):
            continue                                   # 旧版 parquet(无raw列)跳过
        g = df[["high", "low", "close", "volume"]].copy()
        g["j"] = df["kdj_j"]
        sig = L.signal(g, DEFAULT_CFG)
        if not sig.any():
            continue
        env = sh_bull.reindex(g.index).ffill().fillna(False)
        fs = feature_series(g)
        for i in np.flatnonzero(sig.values):
            o = replay_outcome(g, i)
            if o is None:
                continue
            key = bucket_key(bool(env.iloc[i]), fs["j_low"].iloc[i], fs["dd"].iloc[i])
            st = stat.setdefault(key, {"n": 0, "hit": 0, "sum_mu_hit": 0.0,
                                       "sum_md_miss": 0.0, "types": {}})
            st["n"] += 1
            st["types"][o["type"]] = st["types"].get(o["type"], 0) + 1
            if o["type"] == "hit":
                st["hit"] += 1
                st["sum_mu_hit"] += o["max_up"]
            if o["type"] == "miss":
                st["sum_md_miss"] += o["min_dn"]

    buckets = {}
    for k, st in stat.items():
        hits, misses = st["hit"], st["types"].get("miss", 0)
        payoff = ((st["sum_mu_hit"] / hits) / abs(st["sum_md_miss"] / misses)
                  if hits and misses else None)
        buckets["|".join(k)] = {"n": st["n"], "win_rate": st["hit"] / st["n"],
                                "payoff": payoff, "types": st["types"]}
    out = {"cfg_version": cfg_ver, "buckets": buckets}
    with open(lookup_path(cfg_ver, out_dir), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def load_lookup(ver):
    p = lookup_path(ver)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest pool/tests/test_lookup.py -q`
Expected: 4 PASS

- [ ] **Step 5: 提交**

```bash
git add pool/lookup.py pool/tests/test_lookup.py
git commit -m "feat: lookup同型桶——特征/回放/8桶统计(cfg_version键控)" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: scan.py 信号复算 + 初筛排序

**Files:**
- Create: `pool/scan.py`
- Test: `pool/tests/test_scan_screen.py`

**Interfaces:**
- Consumes: `kdj.layers.signal/DEFAULT_CFG`、`pool.lookup`（bucket_key/feature_series）、`pool.cfg`
- Produces:
  - `assemble(df) -> df2`（parquet → layers 输入：取 `open/high/low/close/volume` + `j=kdj_j`）
  - `iter_whitelist(parq_dir) -> list[str]`（`^(sh60|sz00)` 符号）
  - `collect_candidates(parq_dir, expected_date, signal_fn=None, tail=500) -> (cands, anomalies)`；cand dict：`{sym, date, close, prev_close, chg, amount, feats{j_low,dd,age,shrink}}`
  - `env_label(parq_dir) -> (bull: bool, date: str, chg: float)`（sh000300 vs ma240）
  - `screen(cands, lookup_data, top=30, min_amount=2e8, max_chg=0.097) -> (top_cands, n_rejected)`（硬过滤 + 桶胜率排序：n≥30 用桶 win_rate，<30 置中位；并列按 amount 降序；cand 附 `bucket`/`bucket_stat`/`score`）

- [ ] **Step 1: 写失败测试 pool/tests/test_scan_screen.py**

```python
# -*- coding: utf-8 -*-
import pandas as pd
import pytest

from pool import scan as S
from pool.tests.conftest import make_df, write_pq


def test_assemble_maps_j():
    df = make_df(n=30)
    g = S.assemble(df)
    assert list(g.columns) == ["open", "high", "low", "close", "volume", "j"]
    assert (g["j"] == df["kdj_j"]).all()


def test_collect_uses_injected_signal_and_tail(tmp_path, monkeypatch):
    import kdj.layers as L
    parq = tmp_path / "ind"; parq.mkdir()
    write_pq(parq / "sh600001.parquet", make_df(seed=1))
    write_pq(parq / "sz000002.parquet", make_df(seed=2))
    write_pq(parq / "sz300999.parquet", make_df(seed=3))        # 创业板:白名单外
    write_pq(parq / "sh000300.parquet", make_df(seed=4))

    def last_row_only(df, cfg):
        s = pd.Series(False, index=df.index)
        s.iloc[-1] = True
        return s
    cands, anomalies = S.collect_candidates(str(parq), expected_date=None, signal_fn=last_row_only)
    syms = [c["sym"] for c in cands]
    assert syms == ["sh600001", "sz000002"]                     # 白名单+信号,指数/创业板排除
    c = cands[0]
    assert {"j_low", "dd", "age", "shrink"} <= set(c["feats"])
    assert c["amount"] > 0


def test_screen_hard_filter_and_rank():
    tbl = {"bull|j<=10|dd>-15": {"n": 100, "win_rate": 0.6, "payoff": 2.0, "types": {}},
           "bear|10<j<=15|dd<=-15": {"n": 10, "win_rate": 0.9, "payoff": None, "types": {}}}
    lookup_data = {"cfg_version": "x", "buckets": tbl}
    mk = lambda sym, amount, chg, bucket: {"sym": sym, "amount": amount, "chg": chg,
                                           "bucket": bucket, "feats": {}, "close": 10, "date": "d"}
    cands = [mk("a", 3e8, 0.01, "bull|j<=10|dd>-15"),      # n=100桶 胜率0.6
             mk("b", 5e8, 0.005, "bull|j<=10|dd>-15"),      # 同桶,额大 → 排前
             mk("c", 9e8, 0.20, "bull|j<=10|dd>-15"),       # 涨停代理≥9.7% → 剔除
             mk("d", 1e7, 0.01, "bull|j<=10|dd>-15"),       # 成交额<2亿 → 剔除
             mk("e", 4e8, 0.01, "bear|10<j<=15|dd<=-15")]   # n=10<30 → 中位(0.6)
    top, nrej = S.screen(cands, lookup_data, top=3)
    assert nrej == 2 and [t["sym"] for t in top] == ["b", "a", "e"]
    assert top[2]["score"] == pytest.approx(0.6)             # e 拿中位分


def test_screen_short_list_kept():
    lookup_data = {"cfg_version": "x", "buckets": {}}
    cands = [{"sym": "a", "amount": 3e8, "chg": 0.0, "bucket": "bull|j<=10|dd>-15",
              "feats": {}, "close": 10, "date": "d"}]
    top, nrej = S.screen(cands, lookup_data, top=30)
    assert len(top) == 1 and nrej == 0                       # 不足30全保留,如实列数
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest pool/tests/test_scan_screen.py -q`
Expected: FAIL（ModuleNotFoundError: pool.scan）

- [ ] **Step 3: 实现 pool/scan.py（筛选核心；日报/CLI 在 Task 6）**

```python
# -*- coding: utf-8 -*-
"""scan.py —— 每日确定性管道:信号复算→硬过滤→桶排序→池内跟踪→日报(spec §4)。
唯一数据源=indicators parquet(路线B);layers.signal 原封复用,尾部策略滚动现场算。"""
import glob
import os
import statistics
import sys

import pandas as pd

from pool import cfg as C
from pool.lookup import bucket_key, feature_series
from pool import pool as P

RAW_COLS = ["open", "high", "low", "close", "volume"]
MIN_AMOUNT = 2e8          # 流动性:当日成交额(真实,元)
MAX_CHG = 0.097           # 收盘涨停代理(≥9.7% 剔除,次日大概率买不进)
TAIL = 500                # 回看窗口(MA240+EMA收敛余量)


def assemble(df):
    """parquet → layers 输入:原始5列 + j=kdj_j(同批量口径,零重算)。"""
    g = df[RAW_COLS].copy()
    g["j"] = df["kdj_j"]
    return g


def iter_whitelist(parq_dir=None):
    parq_dir = parq_dir or C.PARQ_DIR
    for fp in sorted(glob.glob(os.path.join(parq_dir, "*.parquet"))):
        sym = os.path.basename(fp)[:-8]
        if sym.startswith(("sh60", "sz00")):
            yield fp, sym


def env_label(parq_dir=None):
    """sh000300 vs MA240 → (bull, 指数末日, 指数日涨跌)。指数数据可能滞后一天(已知)。"""
    parq_dir = parq_dir or C.PARQ_DIR
    sh = pd.read_parquet(os.path.join(parq_dir, "sh000300.parquet"))
    bull = bool(sh["close"].iloc[-1] > sh["ma240"].iloc[-1])
    chg = float(sh["close"].iloc[-1] / sh["close"].iloc[-2] - 1)
    return bull, sh.index[-1], chg


def collect_candidates(parq_dir=None, expected_date=None, signal_fn=None, tail=TAIL):
    """复算当日信号并提取特征。signal_fn 可注入(测试);expected_date 非 None 时
    末日不符的票记为异常(停牌/滞后)。返回 (cands, anomalies)。"""
    import kdj.layers as L
    from kdj.layers import DEFAULT_CFG
    parq_dir = parq_dir or C.PARQ_DIR
    sig_fn = signal_fn or (lambda g, cfg: L.signal(g, cfg))
    cands, anomalies = [], []
    for fp, sym in iter_whitelist(parq_dir):
        try:
            df = pd.read_parquet(fp)
            if expected_date and df.index[-1] != expected_date:
                anomalies.append((sym, "末日 %s ≠ %s(停牌/滞后)" % (df.index[-1], expected_date)))
                continue
            g = assemble(df.tail(tail))
            sig = sig_fn(g, DEFAULT_CFG)
            if not bool(sig.iloc[-1]):
                continue
            prev = g["close"].iloc[-2]
            cur = g["close"].iloc[-1]
            cands.append({"sym": sym, "date": g.index[-1], "close": float(cur),
                          "prev_close": float(prev), "chg": float(cur / prev - 1),
                          "amount": float(df["amount"].iloc[-1] / df["factor"].iloc[-1] * 1000),
                          "feats": {k: (float(v) if v == v else None)
                                    for k, v in feature_series(g).items()}})
        except Exception as e:                                  # 单票异常不炸整体
            anomalies.append((sym, repr(e)))
    return cands, anomalies


def screen(cands, lookup_data, top=30, min_amount=MIN_AMOUNT, max_chg=MAX_CHG):
    """硬过滤(成交额/涨停代理)+ 同型桶胜率排序(spec §6)。返回 (top_list, 剔除数)。"""
    alive = [c for c in cands if c["amount"] >= min_amount and c["chg"] < max_chg]
    n_rej = len(cands) - len(alive)
    tbl = lookup_data.get("buckets", {})
    scores = [b["win_rate"] for b in tbl.values() if b.get("n", 0) >= 30]
    med = statistics.median(scores) if scores else 0.5         # <30 样本桶置中位不加分
    for c in alive:
        c["bucket_stat"] = tbl.get(c["bucket"])
        n = c["bucket_stat"]["n"] if c["bucket_stat"] else 0
        c["score"] = c["bucket_stat"]["win_rate"] if c["bucket_stat"] and n >= 30 else med
    alive.sort(key=lambda c: (-c["score"], -c["amount"]))      # 并列按成交额降序
    return alive[:top], n_rej
```

（`cands` 进入 screen 前需已带 `bucket` 键——`main_scan` 里用 `bucket_key(env, j_low, dd)` 标注后再排序；测试里手工给 `bucket`。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest pool/tests/test_scan_screen.py -q`
Expected: 4 PASS

- [ ] **Step 5: 提交**

```bash
git add pool/scan.py pool/tests/test_scan_screen.py
git commit -m "feat: scan信号复算+硬过滤+桶排序(白名单/异常票收集)" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: scan.py 画像卡 + 日报 + snapshot + 主流程

**Files:**
- Modify: `pool/scan.py`
- Test: `pool/tests/test_scan_report.py`

**Interfaces:**
- Consumes: Task 3 `P.track/P.watching`、Task 4 `load_lookup`、Task 5 全部
- Produces:
  - `profile_card(cand, name="") -> str`（md 卡：收盘/涨跌/现价/环境/特征/桶统计/成交额）
  - `main_scan(parq_dir=None, pool_path=None, report_dir=None, now_date=None) -> int`（完整管道：滞后检查→候选→排序→跟踪结案→写 `reports/<日>.md` + `reports/candidates-<日>.json` + pool.json；滞后返回 1）
  - CLI：`python scan.py`（跑当日）；`python scan.py snapshot <code> [--date YYYY-MM-DD]`（JSON 取数，skill 用）
  - candidates JSON：`{"date", "cfg_version", "candidates": [{sym, close, raw_close, chg, amount, bucket, score, bucket_stat, feats}]}`

- [ ] **Step 1: 写失败测试 pool/tests/test_scan_report.py**

```python
# -*- coding: utf-8 -*-
import json

import pandas as pd

from pool import scan as S
from pool.tests.conftest import make_df, write_pq


def test_profile_card_renders():
    cand = {"sym": "sz000651", "close": 38.2, "chg": 0.012, "amount": 5.4e8, "raw_close": 38.2,
            "bucket": "bull|j<=10|dd>-15",
            "bucket_stat": {"n": 120, "win_rate": 0.58, "payoff": 1.9},
            "feats": {"j_low": 8.3, "dd": -0.12, "age": 23, "shrink": 0.72}}
    card = S.profile_card(cand, name="格力电器")
    assert "sz000651" in card and "格力电器" in card
    assert "58.0%" in card and "8.3" in card and "5.4" in card


def _mk_parq(root, last="2026-09-17"):
    parq = root / "ind"; parq.mkdir(exist_ok=True)
    sh = make_df(n=600)
    sh.index = pd.bdate_range(end=last, periods=600).strftime("%Y-%m-%d")
    sh["close"] = 100.0; sh["ma240"] = 90.0                    # 恒牛
    write_pq(parq / "sh000300.parquet", sh)
    for sym, seed in (("sh600000", 1), ("sz000651", 2)):       # 哨兵股=真实扫描的滞后检查对象
        df = make_df(seed=seed)
        df.index = pd.bdate_range(end=last, periods=600).strftime("%Y-%m-%d")
        write_pq(parq / ("%s.parquet" % sym), df)
    return parq


def test_main_scan_e2e_fixture(tmp_path, monkeypatch):
    import kdj.layers as L
    parq = _mk_parq(tmp_path)

    def every_3rd_plus_last(df, cfg):
        s = pd.Series(False, index=df.index)
        s.iloc[::3] = True
        s.iloc[-1] = True                                       # 保证末日有信号
        return s
    monkeypatch.setattr(L, "signal", every_3rd_plus_last)
    pool_path = str(tmp_path / "pool.json")
    rc = S.main_scan(parq_dir=str(parq), pool_path=pool_path,
                     report_dir=str(tmp_path / "reports"), now_date=last)
    assert rc == 0
    rep = tmp_path / "reports" / ("%s.md" % last)
    cj = tmp_path / "reports" / ("candidates-%s.json" % last.replace("-", ""))
    assert rep.is_file() and cj.is_file()
    data = json.load(open(cj))
    assert data["date"] == last and data["cfg_version"]
    assert all(c["amount"] >= S.MIN_AMOUNT or True for c in data["candidates"])
    md = rep.read_text(encoding="utf-8")
    for sec in ("环境", "今日候选", "池内动态", "今日结案", "异常"):
        assert sec in md


def test_main_scan_stale_data_exit1(tmp_path, monkeypatch):
    import kdj.layers as L
    parq = _mk_parq(tmp_path, last="2026-09-15")               # 数据停在两天前
    monkeypatch.setattr(L, "signal", lambda df, cfg: pd.Series(False, index=df.index))
    rc = S.main_scan(parq_dir=str(parq), pool_path=str(tmp_path / "pool.json"),
                     report_dir=str(tmp_path / "reports"), now_date="2026-09-17")
    assert rc == 1                                              # 数据滞后 → 退出码1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest pool/tests/test_scan_report.py -q`
Expected: FAIL（AttributeError: profile_card）

- [ ] **Step 3: 实现——scan.py 追加**

```python
# (接筛选核心) —— 画像卡/主流程/CLI
PARQ_TAIL = TAIL          # 测试可覆写
SENTINELS = ("sh600000", "sz000651")     # 滞后哨兵:几乎不停牌的活跃股


def _read_calendar():
    with open(C.CALENDAR, encoding="utf-8") as f:
        return [x for x in f.read().split() if x]


def profile_card(cand, name=""):
    b = cand.get("bucket_stat") or {}
    wr = "%.1f%%" % (100 * b["win_rate"]) if b.get("win_rate") is not None else "—"
    po = "%.2f" % b["payoff"] if b.get("payoff") is not None else "—"
    f = cand["feats"]
    return ("- **%s %s** 收盘 %.2f(%+.1f%%)/现价 %.2f | 环境 %s | J低 %.1f | 回撤 %.1f%% | "
            "趋势年龄 %s | 缩量比 %s | 桶 %s(胜率 %s 盈亏比 %s 样本 %s) | 成交额 %.1f亿"
            % (cand["sym"], name, cand["close"], 100 * cand["chg"], cand.get("raw_close", 0.0),
               cand.get("env", "—"), f.get("j_low") if f.get("j_low") is not None else float("nan"),
               100 * f["dd"] if f.get("dd") is not None else float("nan"),
               f.get("age"), f.get("shrink"), cand.get("bucket", "—"), wr, po, b.get("n", 0),
               cand["amount"] / 1e8))


def _tracking_bars(df, entry, today):
    """该票 > last_date 且 > added 的K线(后复权)+ T+1 起至今交易日数(按市场日历)。"""
    cal = _read_calendar()
    lo = max(entry["added"], entry["last_date"] or entry["added"])
    bars = [(d, float(df.at[d, "high"]), float(df.at[d, "low"]), float(df.at[d, "close"]))
            for d in df.index if d > lo and d <= today]
    i_add = cal.index(entry["added"]) if entry["added"] in cal else None
    i_today = cal.index(today) if today in cal else None
    n_elapsed = (i_today - i_add) if (i_add is not None and i_today is not None) else len(bars)
    return bars, max(n_elapsed, 0)


def main_scan(parq_dir=None, pool_path=None, report_dir=None, now_date=None):
    parq_dir = parq_dir or C.PARQ_DIR
    pool_path = pool_path or C.POOL_PATH
    report_dir = report_dir or C.REPORT_DIR
    ver = C.cfg_version()
    cal = _read_calendar()
    expected = now_date or cal[-1]
    os.makedirs(report_dir, exist_ok=True)

    # 数据滞后检查(哨兵股)
    stale = [s for s in SENTINELS
             if (lambda p: not os.path.isfile(p)
                 or pd.read_parquet(p).index[-1] < expected)
             (os.path.join(parq_dir, "%s.parquet" % s))]
    if stale:
        with open(os.path.join(report_dir, "%s.md" % expected), "w", encoding="utf-8") as f:
            f.write("# 观察池日报 %s\n\n⚠ 数据滞后:哨兵 %s 未到 %s,请先跑 daily_update.sh\n"
                    % (expected, ",".join(stale), expected))
        print("数据滞后(%s)" % stale)
        return 1

    bull, sh_date, sh_chg = env_label(parq_dir)
    lk = load_lookup(ver)
    if lk is None:
        print("lookup 表缺失,首次生成(慢)…")
        lk = build_lookup(parq_dir=parq_dir, cfg_ver=ver)

    cands, anomalies = collect_candidates(parq_dir, expected_date=expected)
    for c in cands:
        c["env"] = "牛性" if bull else "熊性"
        c["bucket"] = "|".join(bucket_key(bull, c["feats"]["j_low"], c["feats"]["dd"]))
        fp = os.path.join(parq_dir, "%s.parquet" % c["sym"])
        pdf = pd.read_parquet(fp, columns=["factor"])
        c["raw_close"] = float(c["close"] / pdf["factor"].iloc[-1])
    top, n_rej = screen(cands, lk)

    # 池内跟踪与结案
    state = P.load_pool(pool_path)
    closed_today = []
    for e in P.watching(state):
        fp = os.path.join(parq_dir, _sym_path(e["code"]))
        try:
            df = pd.read_parquet(fp)
        except FileNotFoundError:
            anomalies.append((e["code"], "parquet 缺失"))
            continue
        bars, n_el = _tracking_bars(df, e, expected)
        o = P.track(e, bars, n_el)
        if o:
            closed_today.append((e, o))
    P.save_pool(state, pool_path)

    # candidates JSON(入池校验的机器可读源)
    cj = {"date": expected, "cfg_version": ver,
          "candidates": [{k: c.get(k) for k in
                          ("sym", "close", "raw_close", "chg", "amount", "bucket", "score",
                           "bucket_stat", "feats", "env")} for c in top]}
    with open(os.path.join(report_dir, "candidates-%s.json" % expected.replace("-", "")),
              "w", encoding="utf-8") as f:
        json.dump(cj, f, ensure_ascii=False, indent=1)

    # 日报(spec §8)
    lines = ["# 观察池日报 %s" % expected, "",
             "## 1. 环境",
             "沪深300 vs MA240:**%s**(%s 收盘,日涨跌 %+.2f%%;指数数据日 %s)"
             % ("牛性" if bull else "熊性", sh_date, 100 * sh_chg, sh_date), "",
             "## 2. 今日候选",
             "信号 %d 只;硬过滤剔除 %d;取前 %d(不足则如实列数:本日 %d 只)"
             % (len(cands), n_rej, len(top), len(top), len(top)), ""]
    lines += [profile_card(c) for c in top] or ["- 无"]
    lines += ["", "## 3. 池内动态"]
    w = P.watching(state)
    lines += ["- %s 入池 %s 基准 %.2f | 极值 %+.1f%%/%+.1f%% | 已过 %d 日"
              % (e["code"], e["added"], e["add_close"], 100 * e["max_up"], 100 * e["min_dn"],
                 e.get("days", 0)) for e in w] or ["- 池空"]
    lines += ["", "## 4. 今日结案"]
    lines += ["- %s → **%s**(历时 %d 日,极值 %+.1f%%/%+.1f%%)"
              % (e["code"], o["type"], o["days"], 100 * o["max_up"], 100 * o["min_dn"])
              for e, o in closed_today] or ["- 无"]
    lines += ["", "## 5. 异常票 / 数据说明"]
    lines += ["- %s: %s" % a for a in anomalies] or ["- 无"]
    lines += ["", "## 6. AI 研究区", "", ">(\"研究今天的候选\"后由 AI 追加研究卡与推荐理由)", ""]
    with open(os.path.join(report_dir, "%s.md" % expected), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("scan 完成:候选 %d(剔 %d) 结案 %d → %s"
          % (len(top), n_rej, len(closed_today), os.path.join(report_dir, expected + ".md")))
    return 0


def _sym_path(code):
    pre = "sh" if code.startswith("6") else "sz"
    return "%s%s.parquet" % (pre, code)


def snapshot(code, date=None):
    """单票取数(skill 专用,JSON):现价/涨跌/技术快照/环境。"""
    fp = os.path.join(C.PARQ_DIR, _sym_path(code))
    df = pd.read_parquet(fp)
    i = list(df.index).index(date) if date else -1
    g = assemble(df)
    from pool.lookup import signal_features
    feats = signal_features(g, i)
    bull, _, _ = env_label()
    prev = df["close"].iloc[i - 1]
    return {"code": code, "date": df.index[i],
            "close": float(df["close"].iloc[i]),
            "raw_close": float(df["close"].iloc[i] / df["factor"].iloc[i]),
            "chg": float(df["close"].iloc[i] / prev - 1),
            "amount": float(df["amount"].iloc[i] / df["factor"].iloc[i] * 1000),
            "env": "牛性" if bull else "熊性", "feats": feats,
            "ma240": float(df["ma240"].iloc[i]) if "ma240" in df else None,
            "boll_mid": float(df["boll_mid"].iloc[i]) if "boll_mid" in df else None,
            "atr14": float(df["atr14"].iloc[i]) if "atr14" in df else None,
            "macd_hist": float(df["macd_hist"].iloc[i]) if "macd_hist" in df else None}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("snapshot")
    sp.add_argument("code")
    sp.add_argument("--date", default=None)
    a = ap.parse_args()
    if a.cmd == "snapshot":
        print(json.dumps(snapshot(a.code, a.date), ensure_ascii=False))
    else:
        sys.exit(main_scan())
```

注意：
- `_tracking_bars` 里 entry 用 `code` 字段（六位），parquet 文件名带前缀 → `_sym_path`
- main_scan 中 `now_date` 参数供测试注入"今天"；生产取日历末日
- `snapshot` 的 i 为 -1 时 `g.head(len(df))` 即全量（保留尾部语义）

- [ ] **Step 4: 跑全部测试确认通过**

Run: `.venv/bin/python -m pytest pool/tests stockdata/tests -q`
Expected: 全 PASS（零回归红线）

- [ ] **Step 5: 提交**

```bash
git add pool/scan.py pool/tests/test_scan_report.py
git commit -m "feat: scan主流程——画像卡/六栏日报/跟踪结案/snapshot取数/滞后检查" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: daily_update.sh 尾挂 + 真实数据端到端

**Files:**
- Modify: `stockdata/daily_update.sh`

**Interfaces:**
- Consumes: Task 6 `main_scan`（经 `python scan.py`）
- Produces: 每日自动链：数据→指标→观察池扫描；scan 失败不影响主链（仅告警）

- [ ] **Step 1: daily_update.sh 在指标计算成功后、日志轮转前插入**

```bash
# Step 2.5: 观察池每日扫描(失败不影响主链——池是附加层)
echo "[Step 2.5] 观察池扫描..."
if "$VENV_PY" "/home/admin/stock_selection/pool/scan.py"; then
    echo "[✓] 观察池扫描完成"
else
    echo "⚠ 观察池扫描失败(不影响数据与指标)——可单独重跑 pool/scan.py"
fi
```

- [ ] **Step 2: 语法检查**

Run: `bash -n stockdata/daily_update.sh`
Expected: 无输出（语法 OK）

- [ ] **Step 3: 真实数据首跑（含 lookup 首次生成，预计 5-15 分钟）**

```bash
cd /home/admin/stock_selection && .venv/bin/python pool/scan.py
```
Expected: 退出码 0（或 1=数据滞后，今天是 09-17 数据已到位应为 0）；生成 `pool/reports/2026-09-17.md` 与 `pool/reports/candidates-20260917.json`；`pool/lookup_tables/lookup-kdj-default-*.json` 生成

- [ ] **Step 4: 验收 §13.1——日报含 30 张本地卡（或如实列数）**

```bash
grep -c "^- \*\*" pool/reports/2026-09-17.md
head -30 pool/reports/2026-09-17.md
```
Expected: 卡片数 = len(candidates)（≤30，如实）；日报六栏齐全

- [ ] **Step 5: 跑全部测试（零回归）+ 提交**

```bash
.venv/bin/python -m pytest pool/tests stockdata/tests -q
git add stockdata/daily_update.sh
git commit -m "feat: daily_update尾挂观察池扫描(容错不阻断主链)" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: AI 对话层 skill + 验收

**Files:**
- Create: `.claude/skills/pool/SKILL.md`（**用 skill-creator skill 产出**——这是用户钉死的偏好，不许手写）
- Create: `pool/README.md`（模块速览：三模块职责/口径/测试跑法）

**Interfaces:**
- Consumes: 全部前序产物
- Produces: 四句话 skill——"研究今天的候选"（读日报→联网反证→研究卡 append 进日报 §6→推荐理由草稿）；"入池 <codes>"（读 candidates JSON 校验→`snapshot` 取数→`pool.py` 写 pool.json，理由可改）；"复盘"（读 pool.json 只统计当前 cfg_version→review-YYYY-MM-DD.md）；"标注买入 <code> <price>"（只更新 trade 字段）

- [ ] **Step 1: 用 Skill 工具调用 skill-creator:skill-creator，产出 skill**

skill 内容必须覆盖（草稿要点交给 skill-creator 流程）：
- 触发词：研究候选/入池/复盘/标注买入/观察池
- 硬规则：入池只认当日/昨日 candidates JSON；skill 不自己算指标（只调 `scan.py snapshot`）；AI 产出写 md 不进 JSON；复盘只统计当前 cfg_version
- 命令速查：`scan.py snapshot <code>` 取数；pool.json 路径；reason_tags 受控词表（缩量回调/J首拐/板块联动/大盘环境/技术形态/事件驱动）

- [ ] **Step 2: 验收 §13.2——三种结案路径模拟（用真实 pool.json 演练）**

```bash
.venv/bin/python -c "
from pool import pool as P
from pool.cfg import cfg_version
pool = P.load_pool('pool/pool.json')
# 模拟入池(以今日候选第一只为对象)
import json, glob
cj = json.load(open(glob.glob('pool/reports/candidates-*.json')[-1]))
c = cj['candidates'][0]
e = P.add_entry(pool, c['sym'][2:], '', '演练', [], {'env': c['env']}, c['close'],
                cj['date'], [cj['date']], cfg_version())
print('入池演练:', e['code'], '基准', e['add_close'])
P.save_pool(pool, 'pool/pool.json')
"
```
然后手工构造 bars 走 hit/miss/flat 三路（单测已覆盖逻辑，此步验真 JSON 往返），完成后把演练条目从 pool.json 移除。

- [ ] **Step 3: 验收 §13.3/4/5 核对**

- 复盘演练：对话"复盘"→ 生成 review-*.md（当前版本案例数可为 0，报告须如实说明"当前 cfg_version 无已结案例"）
- 版本隔离：改 DEFAULT_CFG 任一参数（如 depth）→ `cfg_version()` 变化 → 旧 lookup/案例统计不混入（单测已锁，此处抽查 `cfg_version()` 输出变化即可，改回）
- 零回归：`.venv/bin/python -m pytest pool/tests stockdata/tests -q` 全绿

- [ ] **Step 4: 提交**

```bash
git add .claude/skills/pool/SKILL.md pool/README.md
git commit -m "feat: 观察池AI对话skill(研究/入池/复盘/标注)+模块README" -m "Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Self-Review 记录

- **Spec 覆盖**：§4 数据流(Task 5/6/7)、§5 数据模型(Task 2/3)、§6 初筛(Task 4/5)、§7 跟踪结案(Task 3)、§8 日报(Task 6)、§9 AI 层+入池校验(Task 8)、§10 错误处理(Task 2 备份恢复/Task 6 滞后+异常栏)、§11 测试(各 Task 内嵌+零回归红线)、§13 验收(Task 7/8) ✓；§12 不做项未引入 ✓
- **口径修正**（对 spec 的实现级细化）：成交额用原生 `amount`（弃"现价×量近似"）；现价=close/factor；桶排序中位数取"n≥30 桶的 win_rate 中位"
- **已知限制**（记录进 README）：sh000300 指数数据滞后一天（env 标签慢一天，可接受）；outcome.days 对停牌票可能少计（信息性字段）；本地无股票名源
- **类型一致性**：track(entry, bars, n_elapsed) 在 Task 3 定义、Task 6 `_tracking_bars` 按此产出 ✓；bucket_key 三元组→"|".join 为 JSON 键，screen 读 `c["bucket"]` 同构 ✓

---

### Task 3a: 极值日期戳增量（2026-09-17 用户需求，对已完成 Task 3 的增量）

**Files:**
- Modify: `pool/pool.py`（track 循环 + 新字段）
- Test: `pool/tests/test_pool_state.py`（追加 2 个测试）

**Interfaces:**
- Consumes: Task 3 全部（track/add_entry 语义不变）
- Produces: entry 新增 4 字段：`max_up_day`/`max_up_date`/`min_dn_day`/`min_dn_date`——`*_day` 为自 T+1 起的交易日序号（1 基），`*_date` 为该日日期；极值被严格超越刷新时同步更新；结案冻结；幂等（≤last_date 的 bar 本就跳过）

- [ ] **Step 1: 追加失败测试（test_pool_state.py）**

```python
def test_extreme_day_stamps_follow_refresh():
    e = mk_entry()
    bars = [bar(D[1], 10.2, 9.9, 10.1),      # day1: max_up 2%
            bar(D[2], 10.1, 9.7, 10.0),      # day2: min_dn 3%(不触发)
            bar(D[3], 10.5, 9.95, 10.4)]     # day3: max_up 刷新 5%
    P.track(e, bars, n_elapsed=3)
    assert e["max_up_day"] == 3 and e["max_up_date"] == D[2 + 1]
    assert e["min_dn_day"] == 2 and e["min_dn_date"] == D[1 + 1]
    P.track(e, bars, n_elapsed=3)             # 重跑 → 戳不动
    assert e["max_up_day"] == 3


def test_new_entry_extreme_fields_init_null():
    pool = {"version": 1, "pool": []}
    e = P.add_entry(pool, "000651", "", "", [], {}, 10.0, D[0], [D[0]], cfg_version())
    for k in ("max_up_day", "max_up_date", "min_dn_day", "min_dn_date"):
        assert e[k] is None
```

- [ ] **Step 2: RED** — `.venv/bin/python -m pytest pool/tests/test_pool_state.py -q` 两新测试失败（KeyError: max_up_day）

- [ ] **Step 3: 实现**——add_entry 模板加 4 个 None 字段；track 循环把 max()/min() 聚合改为严格超越式更新并盖戳：

```python
    for d, hi, lo, c in bars:
        if d <= entry["added"] or (entry["last_date"] and d <= entry["last_date"]):
            continue
        entry["days"] += 1
        up, dn = hi / base - 1, lo / base - 1
        if up > entry["max_up"]:
            entry["max_up"] = up
            entry["max_up_day"], entry["max_up_date"] = entry["days"], d
        if dn < entry["min_dn"]:
            entry["min_dn"] = dn
            entry["min_dn_day"], entry["min_dn_date"] = entry["days"], d
        entry["last_date"], entry["last_close"] = d, c
        ...（触发判定与结案不变）
```

注意：days 计数移到极值更新之前（先计本日序号再盖戳）；mk_entry 测试夹具同步加 4 个 None 字段。

- [ ] **Step 4: GREEN + 零回归** — `pool/tests` 全绿、`stockdata/tests pool/tests` 全绿
- [ ] **Step 5: 提交** `feat: 池卡片记录极值出现日/序号(用户复盘需求)` + 尾注

---

### Task 6b: 卡片现价口径显示（2026-09-17 用户需求）

**原则**:信号/排序/极值/结案比例保持后复权;一切人读价格用现价(=复权价÷当日factor)。

**Files:** Modify `pool/scan.py`(profile_card/日报显示), `pool/pool.py`(add_entry 增可选 add_close_raw), Test: `pool/tests/test_scan_report.py` 追加断言

**改动:**
1. profile_card 收盘价显示 raw_close;涨跌幅不变
2. add_entry 增可选参数 add_close_raw(入池日现价,candidates JSON 已有);池内动态"基准"显示它,缺省回退 add_close
3. last_close 显示当日现价(scan 时现算 close/factor),存储保持后复权
4. 除权背离提示:显示价差与 max_up/min_dn 背离显著时日报加"期间除权,比例按后复权"注记
5. 测试:卡片含现价;预置 add_close_raw 的条目显示它

**明示不做**: 前复权 K 线序列变换(复盘画图时另行实现)

---

### Task 6c: 分桶细化 + 涨停口径对称（2026-09-17 用户裁决）

**背景**: 用户质疑判别力——当日环境统一使 8 桶塌缩为 4 个有效桶,桶内仅剩成交额;且"涨停→明天买不进"措辞错误、回放与筛选口径不对称。

**裁决:**
1. 涨停剔除保留,理由改为"收盘涨停代理(≥9.7%)→次日追高不可操作";**lookup 回放同步跳过信号日涨幅 ≥9.7% 的信号**(统计与筛选口径对称);ST 5% 涨停拦不到维持已知局限
2. 分桶细化: J 深度 3 档(≤5 / 5~10 / 10~15) × 回撤 3 档(>-10% / -10~-15% / -15~-25%),加牛熊共 18 桶,单日有效 9 桶;外边界仍跟 cfg(J 15 / depth -0.25),超界钳制不变;桶内排序仍按成交额
3. **lookup 文件名加桶方案版本**:`lookup-<cfg_version>-b2.json`(BUCKET_VER="b2")——桶方案变更不再静默沿用旧表

**Files:** Modify `pool/lookup.py`(bucket_key 分档/J_THR 边界、replay 跳涨停、lookup_path 加 -b2)、`pool/tests/test_lookup.py`(新分档边界/钳制/涨停跳过/文件名)
**不改:** scan.py(bucket_key 元组 arity 不变,main_scan 拼桶名自动适配)、screen 逻辑、结案语义

**测试要点:** J 5/10 边界落档、dd -10/-15 边界落档、超界钳制、replay 对 chg≥9.7% 信号日返回跳过(build_lookup 计数不含)、文件名含 -b2、旧 8 桶文件(无 -b2)不被 load
