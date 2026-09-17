#!/bin/bash
# 分批执行指标计算，每批200只股票，避免OOM
BATCH_SIZE=200
QLIB_URI="/home/admin/stockdata/qlib_bin"
ALL_FILE="$QLIB_URI/instruments/all.txt"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 提取完整符号（首列，已含市场前缀），转小写，过滤空行
mapfile -t SYMS < <(awk -F'\t' '{print tolower($1)}' "$ALL_FILE" | grep -v '^$')

TOTAL=${#SYMS[@]}
echo "共 $TOTAL 只股票，每批 $BATCH_SIZE 只"

for ((i=0; i<TOTAL; i+=BATCH_SIZE)); do
    BATCH=("${SYMS[@]:i:BATCH_SIZE}")
    BATCH_SYMS=$(IFS=,; echo "${BATCH[*]}")
    START=$((i+1))
    END=$((i+${#BATCH[@]}))
    echo "[$(date '+%H:%M:%S')] 处理 $START-$END / $TOTAL ..."
    cd "$SCRIPT_DIR" && python3 build_indicators.py --symbols "$BATCH_SYMS"
    if [ $? -ne 0 ]; then
        echo "⚠ 批次 $START-$END 执行失败，继续下一批..."
    fi
done

echo "✅ 全部分批任务完成"
