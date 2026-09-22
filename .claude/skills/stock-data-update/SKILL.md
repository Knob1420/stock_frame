---
name: stock-data-update
description: A 股每日数据更新与全市场技术指标计算（qlib 数据下载→校验→原子换新→MACD/RSI/KDJ 分批计算）。只要用户提到更新股票数据、每日数据、下载 qlib 数据、重算指标、指标数据过期、数据更新失败排查、查看数据更新到哪天了，或任何涉及 stockdata 数据管道的操作，都使用本 skill——即使用户没有明确说"更新数据"。
---

# A 股每日数据更新与指标计算

一条命令完成全流程。**不要手工分解执行**（单进程全量算指标会 OOM，服务器仅 1.6G 内存）。

## 执行

```bash
bash /home/admin/stock_frame/stockdata/daily_update.sh
```

预期耗时：有新数据约 7-10 分钟（下载 539MB ~25s + 解压校验 + 31 批指标计算 ~6 分钟）；无新数据约 30 秒。

**退出码语义（判断结果的唯一依据）：**

| 码 | 含义 | 处理 |
|----|------|------|
| 0 | 换新+指标完成，或无新数据（节假日/未发布） | 结束，无需任何动作 |
| 2 | 下载/校验/换新/指标失败 | 看 `logs/daily_*.log` 最新一份定位原因，按下节排查 |

## 验证（agent 触发后自查）

```bash
# 1. 数据日历末（应为最近一个交易日）
tail -1 /home/admin/stock_frame/stockdata/qlib_bin/calendars/day.txt
# 2. 指标文件数（应 ≈6100+）与最新落盘时间（应为今天）
ls /home/admin/stock_frame/stockdata/indicators | wc -l
ls -lt /home/admin/stock_frame/stockdata/indicators | head -3
# 3. 今日运行日志
ls -t /home/admin/stock_frame/stockdata/logs/daily_*.log | head -1
```

## 内部流程（排障时才需要读）

`daily_update.sh` 编排两步：

1. **`update_data.py`**（退出码 0=换新 1=无新数据 2=失败）
   gh-proxy 镜像下载 `qlib_bin.tar.gz`（539MB，直连 GitHub 超时）→ gzip 完整性校验 →
   解压至 staging（`filter="data"` 拒绝路径穿越）→ 校验 instruments/样本股 bin/日历末 →
   与现行数据比较（更新才换；`--force` 可强制）→ 原子换新（失败自动回滚）→
   **立即清理** tar.gz / staging / qlib_bin.old（磁盘策略：不留回退窗口，数据可重下）
2. **`run_indicators_batch.sh`**（仅换新成功才跑）
   从 `instruments/all.txt` 读全部符号，200 只/批独立进程调 `build_indicators.py`，
   每股输出一个 parquet，全量覆盖旧文件；单批失败不阻断后续。

## 文件落盘与保留策略

| 路径 | 内容 | 策略 |
|------|------|------|
| `/home/admin/stock_frame/stockdata/qlib_bin/` | 全 A 股日频行情（qlib 二进制，~848M） | 保留，原地换新 |
| `/home/admin/stock_frame/stockdata/{qlib_bin.tar.gz,staging/,qlib_bin.old/}` | 下载包/中转/旧数据 | 每次运行结束自动删除；若运行中断残留，可直接 `rm -rf` |
| `stockdata/indicators/*.parquet` | 每股指标 19 列（~6100 文件，~2.2G） | 保留，每日覆盖重算 |
| `stockdata/logs/` | 运行日志（`日期.log` + `daily_*.log`） | 保留 30 天自动轮转 |

## 指标清单（每股 parquet，index=日期，全历史，通达信口径，共 19 列）

| 列 | 说明 |
|----|------|
| `macd_dif` / `macd_dea` / `macd_hist` | MACD 12/26/9，柱=2×(DIF−DEA) |
| `rsi_14` | RSI，Wilder 平滑 |
| `kdj_k` / `kdj_d` / `kdj_j` | KDJ 9/3/3，J=3K−2D |
| `ma5`/`ma10`/`ma20`/`ma60`/`ma120`/`ma240` | 收盘价均线族（240=年线，项目牛熊口径） |
| `vma5` / `vma20` | 量能均线（缩量/放量判断） |
| `boll_upper` / `boll_mid` / `boll_lower` | BOLL 20 日 ±2×样本标准差(ddof=1) |
| `atr14` | ATR=MA(TR,14) 国内口径（非 Wilder） |

加新指标：`indicators_conf.py` 注册表加一项 + `build_indicators.py` 写纯函数并登记 `FUNCS`。

## 排障

| 症状 | 处置 |
|------|------|
| 下载超时/失败 | gh-proxy 偶发不可用；等 10 分钟重跑即可，或临时换镜像源（改 `update_data.py` 的 `RELEASE_URL`） |
| staging 校验失败 | 传输损坏（rc=2），现行数据未受影响，直接重跑 |
| 换新失败 | 自动回滚，现行数据完好；重跑 |
| 指标计算某批失败 | 日志有 `⚠ 批次 … 失败`；单独重跑 `bash run_indicators_batch.sh`（已算的会覆盖，无重复副作用） |
| 疑似数据损坏 | `python3 update_data.py --force` 强制重下换新 |

## 定时任务

cron 周一至五 18:35 主跑、20:35/21:35 兜底（数据源偶尔晚发布），周一至六 8:05 次晨兜底（前一晚始终未发布的场景）。查看：`crontab -l`。
已成功后的重复运行会走"无新数据"快速退出（约 30 秒），属预期。

## 红线

- 必须 `.venv` 的 python（`daily_update.sh` 已内置绝对路径），系统 python 无 numpy/qlib
- 禁止 `python3 build_indicators.py` 全市场单进程直跑（OOM）；只能经分批脚本
- 预警扫描（`scan_alerts.py`）尚未适配本机（Windows 路径残留），与本管道无关，勿调用

## watch 单元测试

盯盘/backfill/日报渲染的单测（改动 watch/ 下代码后必跑）：

```bash
cd /home/admin/stock_frame/watch && ../.venv/bin/python -m pytest tests/ -v
```
