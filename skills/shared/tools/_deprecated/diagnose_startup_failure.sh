#!/bin/bash
# 启动失败诊断工具
#
# 功能：
# 1. 收集所有节点的启动日志
# 2. 自动分析错误信息
# 3. 匹配已知问题模式
# 4. 生成诊断报告和修复建议

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ========== 默认参数 ==========
NODES=""
CONTAINER=""
OUTPUT=""
SSH_USER="root"

# ========== 参数解析 ==========
while [[ $# -gt 0 ]]; do
    case "$1" in
        --nodes) NODES="$2"; shift 2 ;;
        --container) CONTAINER="$2"; shift 2 ;;
        --output) OUTPUT="$2"; shift 2 ;;
        --ssh-user) SSH_USER="$2"; shift 2 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

if [ -z "$NODES" ] || [ -z "$CONTAINER" ]; then
    echo "用法: $0 --nodes <节点列表> --container <容器名> [选项]"
    echo ""
    echo "必需参数:"
    echo "  --nodes <ip1,ip2,...>    节点列表"
    echo "  --container <name>       容器名"
    echo ""
    echo "可选参数:"
    echo "  --output <file>          输出诊断报告到 JSON 文件"
    echo "  --ssh-user <user>        SSH 用户（默认 root）"
    exit 1
fi

echo "=========================================="
echo "   启动失败诊断"
echo "=========================================="
echo "节点: $NODES"
echo "容器: $CONTAINER"
echo ""

# ========== 已知问题模式库 ==========
declare -a KNOWN_PATTERNS=(
    "ModuleNotFoundError: No module named 'sentencepiece'|dependency_missing|error|缺少依赖包: sentencepiece|docker exec CONTAINER pip install sentencepiece"
    "ModuleNotFoundError: No module named 'tiktoken'|dependency_missing|error|缺少依赖包: tiktoken|docker exec CONTAINER pip install tiktoken"
    "Couldn't instantiate the backend tokenizer|tokenizer_missing|error|tokenizer 文件缺失或损坏|检查模型目录是否包含 tokenizer.json/tokenizer_config.json，或从完整模型目录复制"
    "Following weights were not initialized from checkpoint|plugin_pp_weight_bug|error|vllm_fl 插件在 PP 模式下的权重分片加载 bug|禁用插件 (VLLM_PLUGINS='') 或改用不带插件的镜像"
    "Connection closed by peer.*is_in_the_same_node|worker_missing_headless|error|Worker 节点(node-rank>0)启动命令缺少 --headless，导致其误起 API server 与 master 争抢初始化|确认 worker 节点 vLLM 命令带 --headless（deploy_vllm.py 已自动处理，node_rank>0 时追加）"
    "WorkerProc initialization failed|worker_missing_headless|error|Worker 节点初始化失败，通常因缺少 --headless 或模型路径/GPU 配置各节点不一致|检查 worker 命令带 --headless，且各节点模型路径完全一致（deploy_vllm.py 已验证通过）"
    "TCPStore.*Broken pipe|network_communication|error|节点间通信失败|检查 MASTER_ADDR、MASTER_PORT 和网络连通性"
    "TCPStore.*Connection refused|network_communication|error|Master 节点未就绪|确保 rank=0 节点先启动并监听端口 29500"
    "CUDA out of memory|gpu_oom|error|GPU 显存不足|减少 --max-model-len 或增加 TP/PP 大小"
    "torch.cuda.OutOfMemoryError|gpu_oom|error|GPU 显存不足|减少 batch size 或模型长度"
    "Free memory on device.*less than desired GPU memory utilization|gpu_oom|error|GPU 显存被其他进程占用|检查 nvidia-smi，避开被占用 GPU 或降低 --gpu-memory-utilization"
    "RuntimeError.*NCCL|nccl_error|error|NCCL 通信错误|检查 NCCL_SOCKET_IFNAME 和网络接口配置"
    "Address already in use|port_occupied|error|端口被占用|更换端口或停止占用端口的进程"
    "No such file or directory.*tokenizer|model_file_missing|error|tokenizer 文件缺失|检查模型路径和文件完整性"
    "trust_remote_code.*is False|trust_remote_code|error|需要信任远程代码|添加 --trust-remote-code 参数"
    "TritonCompiler.*compilation failed|triton_compile_error|warning|Triton 编译失败|清理缓存或禁用对应算子"
    "flag_gems.*not found|flaggems_missing|error|FlagGems 未正确安装|重新安装 FlagGems"
    "ImportError.*libcuda.so|cuda_driver_missing|error|CUDA 驱动未找到|检查容器 GPU 挂载和 NVIDIA 驱动"
)

# ========== 解析节点列表 ==========
IFS=',' read -ra NODE_ARRAY <<< "$NODES"

# ========== 步骤 1: 收集日志 ==========
echo "步骤 1/3: 收集启动日志"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

TMP_DIR=$(mktemp -d)
trap "rm -rf $TMP_DIR" EXIT

declare -A LOG_FILES
declare -a ALL_ERRORS

for i in "${!NODE_ARRAY[@]}"; do
    node="${NODE_ARRAY[$i]}"
    rank=$i
    log_file="$TMP_DIR/rank${rank}.log"

    echo "  [Rank $rank] $node..."

    # 尝试多个可能的日志路径
    bash "$SCRIPT_DIR/exec_on_nodes.sh" \
        --nodes "$node" \
        --container "$CONTAINER" \
        --command "cat /flagos-workspace/logs/startup_rank${rank}.log 2>/dev/null || cat /flagos-workspace/logs/startup.log 2>/dev/null || cat /vllm-workspace/logs/startup.log 2>/dev/null || echo 'LOG_NOT_FOUND'" \
        --output "$TMP_DIR/fetch_${rank}.json" \
        --timeout 30 &>/dev/null

    stdout=$(jq -r ".\"$node\".stdout" "$TMP_DIR/fetch_${rank}.json" 2>/dev/null)

    if [ "$stdout" != "LOG_NOT_FOUND" ] && [ -n "$stdout" ]; then
        echo "$stdout" > "$log_file"
        LOG_FILES["$rank"]="$log_file"

        # 提取错误行
        errors=$(grep -iE "error|exception|failed|traceback" "$log_file" 2>/dev/null || true)
        if [ -n "$errors" ]; then
            echo "    ✗ 发现错误信息"
            ALL_ERRORS+=("$errors")
        else
            echo "    ✓ 日志已收集"
        fi
    else
        echo "    ⚠ 日志文件不存在"
    fi
done

echo ""

# ========== 步骤 2: 分析错误 ==========
echo "步骤 2/3: 分析错误信息"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

declare -a MATCHED_ISSUES
declare -A ISSUE_COUNTS

if [ ${#ALL_ERRORS[@]} -eq 0 ]; then
    echo "  ✓ 未发现明显错误"
    echo ""

    # 检查服务是否实际运行
    echo "  检查服务进程状态..."
    bash "$SCRIPT_DIR/exec_on_nodes.sh" \
        --nodes "$NODES" \
        --container "$CONTAINER" \
        --command "ps aux | grep -E 'vllm|sglang' | grep -v grep | wc -l" \
        --output "$TMP_DIR/process_check.json" \
        --timeout 10 &>/dev/null

    all_running=true
    for node in "${NODE_ARRAY[@]}"; do
        proc_count=$(jq -r ".\"$node\".stdout" "$TMP_DIR/process_check.json" 2>/dev/null | tr -d '\n')
        if [ "$proc_count" = "0" ] || [ -z "$proc_count" ]; then
            echo "  ✗ [$node] 服务进程未运行"
            all_running=false
        fi
    done

    if [ "$all_running" = false ]; then
        MATCHED_ISSUES+=('{"category":"process_not_running","severity":"error","message":"服务进程未运行但日志无明显错误","suggestion":"检查启动命令是否正确执行，或查看更早的日志"}')
    fi
else
    # 合并所有错误文本
    all_errors_text="${ALL_ERRORS[*]}"

    # 匹配已知问题模式
    for pattern_def in "${KNOWN_PATTERNS[@]}"; do
        IFS='|' read -r pattern category severity message fix <<< "$pattern_def"

        if echo "$all_errors_text" | grep -qE "$pattern"; then
            echo "  ✓ 匹配问题: $message"

            # 提取证据
            evidence=$(echo "$all_errors_text" | grep -E "$pattern" | head -3 | sed 's/"/\\"/g' | tr '\n' ' ')

            MATCHED_ISSUES+=("{\"category\":\"$category\",\"severity\":\"$severity\",\"message\":\"$message\",\"fix\":\"$fix\",\"evidence\":\"$evidence\"}")

            # 统计问题出现次数
            count=${ISSUE_COUNTS[$category]:-0}
            ISSUE_COUNTS[$category]=$((count + 1))
        fi
    done

    # 如果没有匹配到已知问题
    if [ ${#MATCHED_ISSUES[@]} -eq 0 ]; then
        echo "  ⚠ 未匹配到已知问题模式"

        # 提取最常见的错误关键词
        common_errors=$(echo "$all_errors_text" | grep -oE "\b[A-Z][a-zA-Z]+Error\b|\bException\b|failed" | sort | uniq -c | sort -rn | head -3 | awk '{print $2}')

        evidence=$(echo "$all_errors_text" | head -5 | sed 's/"/\\"/g' | tr '\n' ' ')

        MATCHED_ISSUES+=("{\"category\":\"unknown_error\",\"severity\":\"error\",\"message\":\"未知错误: $common_errors\",\"fix\":\"需要人工分析日志\",\"evidence\":\"$evidence\"}")
    fi
fi

echo ""

# ========== 步骤 3: 确定根因 ==========
echo "步骤 3/3: 确定根本原因"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

ROOT_CAUSE=""
PRIORITY_ORDER=("dependency_missing" "vllm_mp_unstable" "network_communication" "gpu_oom" "model_file_missing")

# 按优先级选择根因
for priority_cat in "${PRIORITY_ORDER[@]}"; do
    for issue in "${MATCHED_ISSUES[@]}"; do
        category=$(echo "$issue" | jq -r '.category')
        if [ "$category" = "$priority_cat" ]; then
            ROOT_CAUSE="$issue"
            break 2
        fi
    done
done

# 如果没有匹配优先级列表，取第一个
if [ -z "$ROOT_CAUSE" ] && [ ${#MATCHED_ISSUES[@]} -gt 0 ]; then
    ROOT_CAUSE="${MATCHED_ISSUES[0]}"
fi

if [ -n "$ROOT_CAUSE" ]; then
    message=$(echo "$ROOT_CAUSE" | jq -r '.message')
    category=$(echo "$ROOT_CAUSE" | jq -r '.category')
    echo "  根本原因: $message"
    echo "  类别: $category"
else
    echo "  无法确定根本原因"
fi

echo ""

# ========== 生成诊断报告 ==========
echo "=========================================="
echo "   诊断结果"
echo "=========================================="

if [ ${#MATCHED_ISSUES[@]} -eq 0 ]; then
    STATUS="inconclusive"
    echo "状态: ⚠ 无明确诊断结果"
else
    STATUS="failed"
    echo "状态: ✗ 启动失败"
fi

echo ""
echo "发现的问题 (${#MATCHED_ISSUES[@]}):"
echo ""

declare -a FIXES
priority=1

for issue in "${MATCHED_ISSUES[@]}"; do
    message=$(echo "$issue" | jq -r '.message')
    severity=$(echo "$issue" | jq -r '.severity')
    fix=$(echo "$issue" | jq -r '.fix')

    echo "  $priority. [$severity] $message"
    echo "     修复: $fix"
    echo ""

    fix_json="{\"priority\":$priority,\"action\":\"$message\",\"command\":\"$fix\"}"
    FIXES+=("$fix_json")

    priority=$((priority + 1))
done

# ========== 检查受影响的节点 ==========
echo "受影响的节点:"
for i in "${!NODE_ARRAY[@]}"; do
    node="${NODE_ARRAY[$i]}"
    rank=$i

    if [ -f "${LOG_FILES[$rank]}" ]; then
        error_count=$(grep -icE "error|exception|failed" "${LOG_FILES[$rank]}" 2>/dev/null || echo "0")
        if [ "$error_count" -gt 0 ]; then
            echo "  ✗ [Rank $rank] $node ($error_count 个错误)"
        else
            echo "  ✓ [Rank $rank] $node"
        fi
    else
        echo "  ? [Rank $rank] $node (无日志)"
    fi
done

echo ""

# ========== 输出到文件 ==========
if [ -n "$OUTPUT" ]; then
    # 构造 JSON 报告
    issues_json="[$(IFS=,; echo "${MATCHED_ISSUES[*]}")]"
    fixes_json="[$(IFS=,; echo "${FIXES[*]}")]"

    affected_nodes="["
    first=true
    for i in "${!NODE_ARRAY[@]}"; do
        node="${NODE_ARRAY[$i]}"
        rank=$i

        if [ "$first" = false ]; then
            affected_nodes="$affected_nodes,"
        fi
        first=false

        affected_nodes="$affected_nodes\"$node\""
    done
    affected_nodes="$affected_nodes]"

    root_cause_json="null"
    if [ -n "$ROOT_CAUSE" ]; then
        root_cause_json="$ROOT_CAUSE"
    fi

    cat > "$OUTPUT" << EOF
{
  "status": "$STATUS",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "root_cause": $root_cause_json,
  "all_issues": $issues_json,
  "fixes": $fixes_json,
  "affected_nodes": $affected_nodes,
  "related_docs": [
    "docs/MULTI_NODE_TROUBLESHOOTING.md"
  ]
}
EOF

    echo "诊断报告已保存到: $OUTPUT"
    echo ""
fi

# ========== 建议下一步操作 ==========
echo "建议的下一步操作:"
echo ""

if [ ${#FIXES[@]} -gt 0 ]; then
    echo "1. 根据上述修复建议逐一处理"
    echo "2. 修复后重新启动服务"
    echo "3. 如果问题持续，收集完整日志进行人工分析"
else
    echo "1. 收集完整的启动日志和系统日志"
    echo "2. 检查所有节点的环境一致性"
    echo "3. 尝试单节点启动排除分布式通信问题"
fi

echo ""

# ========== 返回退出码 ==========
if [ "$STATUS" = "failed" ]; then
    exit 1
else
    exit 0
fi
