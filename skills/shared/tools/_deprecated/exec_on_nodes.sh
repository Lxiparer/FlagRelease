#!/bin/bash
# 多节点统一命令执行工具
#
# 功能：
# 1. 在所有节点并行/顺序执行命令
# 2. 统一 SSH 封装和错误处理
# 3. 支持超时和重试机制
# 4. 输出 JSON 格式结果

set -euo pipefail

# ========== 默认参数 ==========
NODES=""
CONTAINER=""
COMMAND=""
PARALLEL=true
RETRY=1
TIMEOUT=30
OUTPUT=""
SSH_USER="root"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=5"

# ========== 参数解析 ==========
while [[ $# -gt 0 ]]; do
    case "$1" in
        --nodes) NODES="$2"; shift 2 ;;
        --container) CONTAINER="$2"; shift 2 ;;
        --command) COMMAND="$2"; shift 2 ;;
        --parallel) PARALLEL=true; shift ;;
        --sequential) PARALLEL=false; shift ;;
        --retry) RETRY="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --output) OUTPUT="$2"; shift 2 ;;
        --ssh-user) SSH_USER="$2"; shift 2 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

if [ -z "$NODES" ] || [ -z "$COMMAND" ]; then
    echo "用法: $0 --nodes <节点列表> --command <命令> [选项]"
    echo ""
    echo "必需参数:"
    echo "  --nodes <ip1,ip2,...>    节点列表（逗号分隔）"
    echo "  --command <cmd>          要执行的命令"
    echo ""
    echo "可选参数:"
    echo "  --container <name>       容器名（如果命令需要在容器内执行）"
    echo "  --parallel               并行执行（默认）"
    echo "  --sequential             顺序执行"
    echo "  --retry <N>              失败重试次数（默认 1）"
    echo "  --timeout <seconds>      命令超时（默认 30）"
    echo "  --output <file>          输出结果到 JSON 文件"
    echo "  --ssh-user <user>        SSH 用户（默认 root）"
    exit 1
fi

echo "[exec_on_nodes] 多节点命令执行"
echo "  节点: $NODES"
echo "  命令: $COMMAND"
echo "  模式: $([ "$PARALLEL" = true ] && echo '并行' || echo '顺序')"
echo "  重试: $RETRY"
echo "  超时: ${TIMEOUT}s"
echo ""

# ========== 解析节点列表 ==========
IFS=',' read -ra NODE_ARRAY <<< "$NODES"

# ========== 创建临时目录存储结果 ==========
TMP_DIR=$(mktemp -d)
trap "rm -rf $TMP_DIR" EXIT

# ========== 执行函数 ==========
execute_on_node() {
    local node=$1
    local result_file="$TMP_DIR/${node}.json"

    echo "  [$node] 开始执行..."

    local attempt=1
    local success=false
    local stdout=""
    local stderr=""
    local exit_code=0
    local start_time=$(date +%s.%N)

    while [ $attempt -le $RETRY ]; do
        if [ $attempt -gt 1 ]; then
            echo "  [$node] 重试 $attempt/$RETRY..."
        fi

        # 构造完整命令
        local full_cmd="$COMMAND"
        if [ -n "$CONTAINER" ]; then
            full_cmd="docker exec $CONTAINER bash -c '$COMMAND'"
        fi

        # 执行命令（带超时）
        local exec_start=$(date +%s.%N)
        if timeout $TIMEOUT ssh $SSH_OPTS "${SSH_USER}@${node}" "$full_cmd" > "$TMP_DIR/${node}.stdout" 2> "$TMP_DIR/${node}.stderr"; then
            success=true
            exit_code=0
            stdout=$(cat "$TMP_DIR/${node}.stdout" 2>/dev/null || echo "")
            stderr=$(cat "$TMP_DIR/${node}.stderr" 2>/dev/null || echo "")
            echo "  [$node] ✓ 成功"
            break
        else
            exit_code=$?
            stdout=$(cat "$TMP_DIR/${node}.stdout" 2>/dev/null || echo "")
            stderr=$(cat "$TMP_DIR/${node}.stderr" 2>/dev/null || echo "")

            if [ $exit_code -eq 124 ]; then
                echo "  [$node] ✗ 超时（${TIMEOUT}s）"
                stderr="Command timed out after ${TIMEOUT}s"
            else
                echo "  [$node] ✗ 失败（退出码: $exit_code）"
            fi
        fi

        attempt=$((attempt + 1))
        [ $attempt -le $RETRY ] && sleep 2
    done

    local end_time=$(date +%s.%N)
    local duration=$(echo "$end_time - $start_time" | bc)

    # 转义 JSON 字符串
    stdout_json=$(echo "$stdout" | python3 -c "import sys, json; print(json.dumps(sys.stdin.read()))")
    stderr_json=$(echo "$stderr" | python3 -c "import sys, json; print(json.dumps(sys.stdin.read()))")

    # 写入结果
    cat > "$result_file" << EOF
{
  "node": "$node",
  "status": "$([ "$success" = true ] && echo 'success' || echo 'failed')",
  "stdout": $stdout_json,
  "stderr": $stderr_json,
  "exit_code": $exit_code,
  "duration": $duration,
  "attempts": $((attempt - 1))
}
EOF
}

# ========== 执行命令 ==========
if [ "$PARALLEL" = true ]; then
    # 并行执行
    for node in "${NODE_ARRAY[@]}"; do
        execute_on_node "$node" &
    done
    wait
else
    # 顺序执行
    for node in "${NODE_ARRAY[@]}"; do
        execute_on_node "$node"
    done
fi

# ========== 汇总结果 ==========
echo ""
echo "[exec_on_nodes] 结果汇总:"

# 构造 JSON 输出
results="{"
first=true
success_count=0
failed_count=0

for node in "${NODE_ARRAY[@]}"; do
    result_file="$TMP_DIR/${node}.json"

    if [ -f "$result_file" ]; then
        if [ "$first" = false ]; then
            results="$results,"
        fi
        first=false

        results="$results\"$node\":$(cat $result_file)"

        # 统计成功/失败
        status=$(jq -r '.status' "$result_file")
        if [ "$status" = "success" ]; then
            success_count=$((success_count + 1))
            echo "  [$node] ✓ 成功"
        else
            failed_count=$((failed_count + 1))
            echo "  [$node] ✗ 失败"
            # 显示错误信息
            stderr=$(jq -r '.stderr' "$result_file")
            if [ -n "$stderr" ] && [ "$stderr" != "null" ]; then
                echo "    错误: $stderr" | head -3
            fi
        fi
    fi
done

results="$results}"

echo ""
echo "成功: $success_count / 失败: $failed_count / 总计: ${#NODE_ARRAY[@]}"

# ========== 输出到文件 ==========
if [ -n "$OUTPUT" ]; then
    echo "$results" | jq '.' > "$OUTPUT"
    echo ""
    echo "结果已保存到: $OUTPUT"
fi

# ========== 返回退出码 ==========
if [ $failed_count -gt 0 ]; then
    exit 1
else
    exit 0
fi
