# Task Plan: Watchlist 提醒链路历史回放验证（2020-01 ~ 2026-09）

## Goal
按日回放新提醒链路（规则→计分→门槛→冷却→前3名排序），对比 selected / cap_suppressed /
cooldown_suppressed / 未达标四类股票的前瞻路径（5/10/20 日收益、MFE/MAE），
并检验提醒分与前瞻收益是否单调，产出决策导向报告。

## Phases
- [x] Phase 1: 读 scan.py / rules.py，提取计分、门槛、冷却、排序的精确逻辑
- [x] Phase 2: 写回放脚本 watch/replay.py（序列版规则 + 增量周线 + 日循环状态机）
- [x] Phase 3: 一致性校验通过（1066 条规则末日 + weekly 10018 抽样日 + 端到端 preview）
- [x] Phase 4: 回放完成：62,584 行事件 → temp/replay_events.parquet
  （修复 bug：excess_ret20 基准索引错位，曾取到 mfe20 当基准）
- [x] Phase 5: 报告完成 → watch/REPLAY_VALIDATION.md

## 核心结论
- 达标事件 5/10/20 日截面超额全为负；分数≥10 组最差（10日 −0.78%，t=−4.0）
- 同日配对：selected vs cap 无增量（差 −0.13%/10d，t=−0.66）
- 门槛放行的是短期更弱的票；未达标弱变化超额 ≈ 0
- 冷却拦下的事件与放行者质量相当 → 冷却是纯降噪，保留
- 提醒分不应升级为择股分数；"等待确认"的谨慎口径有数据支撑

## Key Questions
1. selected（前3名）相对 cap_suppressed 是否有扣成本后的收益/风险增量？
2. 提醒分区间与前瞻收益是否单调？
3. 风险信号（box_breakdown）穿透冷却的实际后续路径如何？
4. 冷却机制拦下的提醒质量如何（是否真的低增量）？

## Decisions Made
- 区间：2020-01-01 ~ 2026-09-18（用户确认）
- 对象：全链路回放（用户确认）
- 规则序列在回放中重实现为向量化版本，与 watch/rules.py 末日值抽样断言一致
- watchlist 用当前 152 只构成（承认幸存者/构成偏差，报告中声明）

## Errors Encountered

## Status
**全部完成**（2026-10-06）。产物：watch/replay.py、watch/REPLAY_VALIDATION.md、
temp/replay_events.parquet（可删，可由 replay.py 重建）
