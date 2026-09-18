#!/bin/bash
# =============================================================================
# daily_update.sh —— 每日数据更新 + 指标计算唯一入口(供 cron 与 agent 手动触发)
# 流程: update_data.py(下载→校验→原子换新→立即清理) → rc=0 才分批算指标 → 日志轮转
# 退出码: 0=成功或无新数据  2=数据更新/指标计算失败
# 注意: 不用 set -e——update_data.py 的退出码需要分支处理(1=无新数据属正常)
# =============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="/home/admin/stock_selection/.venv/bin/python3"
LOG_DIR="/home/admin/stockdata/logs"   # 数据与代码分离:日志落仓库外(2026-09-18)
LOG_FILE="$LOG_DIR/daily_$(date '+%Y%m%d_%H%M%S').log"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=== 每日数据更新开始 $(date) ==="

# Step 1: 数据更新(下载→gzip校验→解压→staging校验→原子换新→清理 gz/staging/.old)
"$VENV_PY" "$SCRIPT_DIR/update_data.py"
rc=$?

# Step 2: 仅换新成功(rc=0)才重算指标;rc=1=无新数据(节假日/数据未发布)属正常
if [ "$rc" -eq 0 ]; then
    echo "[Step 2] 数据已换新,分批计算指标(200只/批,独立进程防 OOM)..."
    if bash "$SCRIPT_DIR/run_indicators_batch.sh"; then
        echo "[✓] 指标计算完成"
    else
        echo "⚠ 指标计算失败——数据已就位,可单独重跑 run_indicators_batch.sh"
        exit 2
    fi
elif [ "$rc" -eq 1 ]; then
    echo "[✓] 无新数据,正常结束"
    exit 0
else
    echo "✗ 数据更新失败(rc=$rc),今日跳过指标计算"
    exit 2
fi

# Step 2.5: 观察池每日扫描(失败不影响主链——池是附加层)
echo "[Step 2.5] 观察池扫描..."
if "$VENV_PY" "/home/admin/stock_selection/pool/scan.py"; then
    echo "[✓] 观察池扫描完成"
else
    echo "⚠ 观察池扫描失败(不影响数据与指标)——可单独重跑 pool/scan.py"
fi

# Step 3: 日志轮转(保留 30 天)
find "$LOG_DIR" -name "*.log" -mtime +30 -delete

echo "=== 每日数据更新完成 $(date) ==="
