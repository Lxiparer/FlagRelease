#!/bin/bash
# 集群环境验证工具
#
# 功能：
# 1. 全面检查所有节点的环境状态
# 2. 验证 SSH、容器、镜像、依赖、GPU、网络等
# 3. 生成详细的检查报告和修复建议
# 4. 支持自动修复模式

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ========== 默认参数 ==========
NODES=""
CONTAINER=""
MODEL_SIZE=0
OUTPUT=""
AUTO_FIX=false
SSH_USER="root"

# ========== 参数解析 ==========
while [[ $# -gt 0 ]]; do
    case "$1" in
        --nodes) NODES="$2"; shift 2 ;;
        --container) CONTAINER="$2"; shift 2 ;;
        --model-size) MODEL_SIZE="$2"; shift 2 ;;
        --output) OUTPUT="$2"; shift 2 ;;
        --auto-fix) AUTO_FIX=true; shift ;;
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
    echo "  --model-size <GB>        模型大小（用于显存检查）"
    echo "  --output <file>          输出报告到 JSON 文件"
    echo "  --auto-fix               自动修复可修复的问题"
    echo "  --ssh-user <user>        SSH 用户（默认 root）"
    exit 1
fi

echo "=========================================="
echo "   集群环境验证"
echo "=========================================="
echo "节点: $NODES"
echo "容器: $CONTAINER"
echo "模型大小: ${MODEL_SIZE}GB"
echo "自动修复: $([ "$AUTO_FIX" = true ] && echo '是' || echo '否')"
echo ""

# ========== 辅助函数 ==========
check_pass() {
    echo "✓ $1"
}

check_fail() {
    echo "✗ $1"
}

check_warn() {
    echo "⚠ $1"
}

# ========== 解析节点列表 ==========
IFS=',' read -ra NODE_ARRAY <<< "$NODES"

# ========== 检查结果收集 ==========
declare -A RESULTS
declare -a ISSUES

# ========== 检查 1: SSH 连通性 ==========
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "检查 1/10: SSH 连通性"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

for node in "${NODE_ARRAY[@]}"; do
    echo -n "  [$node] "
    if timeout 5 ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "${SSH_USER}@${node}" "echo ok" &>/dev/null; then
        check_pass "SSH 连通"
        RESULTS["${node}_ssh"]="pass"
    else
        check_fail "SSH 连接失败"
        RESULTS["${node}_ssh"]="failed"
        ISSUES+=("{\"node\":\"$node\",\"check\":\"ssh\",\"severity\":\"error\",\"message\":\"SSH 连接失败\",\"suggestion\":\"检查网络和 SSH 配置\"}")
    fi
done

echo ""

# ========== 检查 2: 容器状态 ==========
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "检查 2/10: 容器状态"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

bash "$SCRIPT_DIR/exec_on_nodes.sh" \
    --nodes "$NODES" \
    --command "docker inspect --format='{{.State.Status}}' $CONTAINER 2>/dev/null || echo 'not_found'" \
    --output /tmp/check_container.json \
    --retry 1 \
    --timeout 10 &>/dev/null

for node in "${NODE_ARRAY[@]}"; do
    status=$(jq -r ".\"$node\".stdout" /tmp/check_container.json 2>/dev/null | tr -d '\n')
    echo -n "  [$node] "

    if [ "$status" = "running" ]; then
        check_pass "容器运行中"
        RESULTS["${node}_container"]="pass"
    elif [ "$status" = "exited" ]; then
        check_warn "容器已停止"
        RESULTS["${node}_container"]="warning"
        ISSUES+=("{\"node\":\"$node\",\"check\":\"container\",\"severity\":\"warning\",\"message\":\"容器已停止\",\"suggestion\":\"docker start $CONTAINER\"}")
    else
        check_fail "容器不存在"
        RESULTS["${node}_container"]="failed"
        ISSUES+=("{\"node\":\"$node\",\"check\":\"container\",\"severity\":\"error\",\"message\":\"容器不存在\",\"suggestion\":\"需要先创建容器\"}")
    fi
done

echo ""

# ========== 检查 3: 镜像一致性 ==========
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "检查 3/10: 镜像一致性"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

bash "$SCRIPT_DIR/exec_on_nodes.sh" \
    --nodes "$NODES" \
    --command "docker inspect --format='{{.Image}}' $CONTAINER 2>/dev/null || echo 'unknown'" \
    --output /tmp/check_image.json \
    --retry 1 \
    --timeout 10 &>/dev/null

declare -A IMAGE_DIGESTS
for node in "${NODE_ARRAY[@]}"; do
    digest=$(jq -r ".\"$node\".stdout" /tmp/check_image.json 2>/dev/null | tr -d '\n')
    IMAGE_DIGESTS["$node"]="$digest"
done

# 检查是否所有节点镜像一致
first_digest="${IMAGE_DIGESTS[${NODE_ARRAY[0]}]}"
all_match=true

for node in "${NODE_ARRAY[@]}"; do
    echo -n "  [$node] "
    digest="${IMAGE_DIGESTS[$node]}"

    if [ "$digest" = "unknown" ]; then
        check_fail "无法获取镜像信息"
        RESULTS["${node}_image"]="failed"
        all_match=false
    elif [ "$digest" = "$first_digest" ]; then
        check_pass "镜像一致 (${digest:0:12}...)"
        RESULTS["${node}_image"]="pass"
    else
        check_fail "镜像不一致 (${digest:0:12}...)"
        RESULTS["${node}_image"]="failed"
        all_match=false
        ISSUES+=("{\"node\":\"$node\",\"check\":\"image\",\"severity\":\"error\",\"message\":\"镜像 digest 不一致\",\"suggestion\":\"确保所有节点使用相同的镜像版本\"}")
    fi
done

echo ""

# ========== 检查 4: Python 环境 ==========
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "检查 4/10: Python 环境"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

bash "$SCRIPT_DIR/exec_on_nodes.sh" \
    --nodes "$NODES" \
    --container "$CONTAINER" \
    --command "PATH=/opt/conda/bin:\$PATH which python3 && PATH=/opt/conda/bin:\$PATH python3 --version" \
    --output /tmp/check_python.json \
    --retry 1 \
    --timeout 10 &>/dev/null

for node in "${NODE_ARRAY[@]}"; do
    echo -n "  [$node] "
    exit_code=$(jq -r ".\"$node\".exit_code" /tmp/check_python.json 2>/dev/null)
    stdout=$(jq -r ".\"$node\".stdout" /tmp/check_python.json 2>/dev/null)

    if [ "$exit_code" = "0" ]; then
        python_path=$(echo "$stdout" | head -1)
        python_version=$(echo "$stdout" | tail -1)
        check_pass "$python_path ($python_version)"
        RESULTS["${node}_python"]="pass"
    else
        check_fail "Python 环境异常"
        RESULTS["${node}_python"]="failed"
        ISSUES+=("{\"node\":\"$node\",\"check\":\"python\",\"severity\":\"error\",\"message\":\"Python 环境不可用\",\"suggestion\":\"检查容器内 /opt/conda/bin/python3\"}")
    fi
done

echo ""

# ========== 检查 5: 依赖包 ==========
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "检查 5/10: 关键依赖包"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

REQUIRED_PACKAGES="torch vllm transformers sentencepiece tiktoken"

for pkg in $REQUIRED_PACKAGES; do
    bash "$SCRIPT_DIR/exec_on_nodes.sh" \
        --nodes "$NODES" \
        --container "$CONTAINER" \
        --command "PATH=/opt/conda/bin:\$PATH python3 -c 'import $pkg; print($pkg.__version__)' 2>/dev/null || echo 'missing'" \
        --output "/tmp/check_${pkg}.json" \
        --retry 1 \
        --timeout 10 &>/dev/null

    for node in "${NODE_ARRAY[@]}"; do
        version=$(jq -r ".\"$node\".stdout" "/tmp/check_${pkg}.json" 2>/dev/null | tr -d '\n')
        echo -n "  [$node] $pkg: "

        if [ "$version" = "missing" ] || [ -z "$version" ]; then
            check_fail "未安装"
            RESULTS["${node}_dep_${pkg}"]="failed"
            ISSUES+=("{\"node\":\"$node\",\"check\":\"dependencies\",\"severity\":\"error\",\"message\":\"缺少依赖包: $pkg\",\"suggestion\":\"docker exec $CONTAINER pip install $pkg\"}")
        else
            check_pass "$version"
            RESULTS["${node}_dep_${pkg}"]="pass"
        fi
    done
    echo ""
done

# ========== 检查 6: GPU 可见性 ==========
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "检查 6/10: GPU 可见性"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

bash "$SCRIPT_DIR/exec_on_nodes.sh" \
    --nodes "$NODES" \
    --container "$CONTAINER" \
    --command "nvidia-smi --query-gpu=index,name --format=csv,noheader 2>/dev/null | wc -l" \
    --output /tmp/check_gpu.json \
    --retry 1 \
    --timeout 15 &>/dev/null

for node in "${NODE_ARRAY[@]}"; do
    echo -n "  [$node] "
    gpu_count=$(jq -r ".\"$node\".stdout" /tmp/check_gpu.json 2>/dev/null | tr -d '\n')

    if [ "$gpu_count" -gt 0 ] 2>/dev/null; then
        check_pass "$gpu_count 张 GPU"
        RESULTS["${node}_gpu"]="pass"
    else
        check_fail "无 GPU 或 nvidia-smi 不可用"
        RESULTS["${node}_gpu"]="failed"
        ISSUES+=("{\"node\":\"$node\",\"check\":\"gpu\",\"severity\":\"error\",\"message\":\"无法检测到 GPU\",\"suggestion\":\"检查 NVIDIA 驱动和容器 GPU 挂载\"}")
    fi
done

echo ""

# ========== 检查 7: GPU 显存 ==========
if [ "$MODEL_SIZE" -gt 0 ]; then
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "检查 7/10: GPU 显存"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    bash "$SCRIPT_DIR/exec_on_nodes.sh" \
        --nodes "$NODES" \
        --container "$CONTAINER" \
        --command "nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | awk '{s+=\$1} END {print s}'" \
        --output /tmp/check_memory.json \
        --retry 1 \
        --timeout 15 &>/dev/null

    REQUIRED_MEMORY=$((MODEL_SIZE * 1024 * 12 / 10))  # 模型大小 × 1.2

    for node in "${NODE_ARRAY[@]}"; do
        echo -n "  [$node] "
        free_memory=$(jq -r ".\"$node\".stdout" /tmp/check_memory.json 2>/dev/null | tr -d '\n')

        if [ "$free_memory" -gt "$REQUIRED_MEMORY" ] 2>/dev/null; then
            free_gb=$((free_memory / 1024))
            check_pass "${free_gb}GB 空闲（需要 $((REQUIRED_MEMORY / 1024))GB）"
            RESULTS["${node}_memory"]="pass"
        else
            free_gb=$((free_memory / 1024))
            check_warn "${free_gb}GB 空闲（需要 $((REQUIRED_MEMORY / 1024))GB）"
            RESULTS["${node}_memory"]="warning"
            ISSUES+=("{\"node\":\"$node\",\"check\":\"memory\",\"severity\":\"warning\",\"message\":\"GPU 显存可能不足\",\"suggestion\":\"清理 GPU 或增加 TP/PP 大小\"}")
        fi
    done

    echo ""
else
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "检查 7/10: GPU 显存（跳过，未指定模型大小）"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
fi

# ========== 检查 8: 网络端口 ==========
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "检查 8/10: 网络端口"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

PORTS="8000 29500"

for port in $PORTS; do
    bash "$SCRIPT_DIR/exec_on_nodes.sh" \
        --nodes "$NODES" \
        --command "netstat -tuln 2>/dev/null | grep -q \":$port \" && echo 'occupied' || echo 'available'" \
        --output "/tmp/check_port_${port}.json" \
        --retry 1 \
        --timeout 10 &>/dev/null

    for node in "${NODE_ARRAY[@]}"; do
        status=$(jq -r ".\"$node\".stdout" "/tmp/check_port_${port}.json" 2>/dev/null | tr -d '\n')
        echo -n "  [$node] Port $port: "

        if [ "$status" = "available" ]; then
            check_pass "可用"
            RESULTS["${node}_port_${port}"]="pass"
        else
            check_warn "已占用"
            RESULTS["${node}_port_${port}"]="warning"
            ISSUES+=("{\"node\":\"$node\",\"check\":\"port\",\"severity\":\"warning\",\"message\":\"端口 $port 已被占用\",\"suggestion\":\"可能是旧服务未停止，建议先清理\"}")
        fi
    done
    echo ""
done

# ========== 检查 9: 模型文件 ==========
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "检查 9/10: 模型文件"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

bash "$SCRIPT_DIR/exec_on_nodes.sh" \
    --nodes "$NODES" \
    --container "$CONTAINER" \
    --command "ls /models/config.json /models/tokenizer_config.json 2>/dev/null | wc -l" \
    --output /tmp/check_model.json \
    --retry 1 \
    --timeout 10 &>/dev/null

for node in "${NODE_ARRAY[@]}"; do
    echo -n "  [$node] "
    file_count=$(jq -r ".\"$node\".stdout" /tmp/check_model.json 2>/dev/null | tr -d '\n')

    if [ "$file_count" -ge 2 ] 2>/dev/null; then
        check_pass "模型文件存在"
        RESULTS["${node}_model"]="pass"
    else
        check_fail "模型文件缺失"
        RESULTS["${node}_model"]="failed"
        ISSUES+=("{\"node\":\"$node\",\"check\":\"model\",\"severity\":\"error\",\"message\":\"模型文件不完整\",\"suggestion\":\"检查模型挂载路径 /models\"}")
    fi
done

echo ""

# ========== 检查 10: 磁盘空间 ==========
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "检查 10/10: 磁盘空间"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

bash "$SCRIPT_DIR/exec_on_nodes.sh" \
    --nodes "$NODES" \
    --container "$CONTAINER" \
    --command "df -BG /flagos-workspace 2>/dev/null | tail -1 | awk '{print \$4}' | tr -d 'G'" \
    --output /tmp/check_disk.json \
    --retry 1 \
    --timeout 10 &>/dev/null

for node in "${NODE_ARRAY[@]}"; do
    echo -n "  [$node] "
    free_space=$(jq -r ".\"$node\".stdout" /tmp/check_disk.json 2>/dev/null | tr -d '\n')

    if [ "$free_space" -gt 50 ] 2>/dev/null; then
        check_pass "${free_space}GB 可用"
        RESULTS["${node}_disk"]="pass"
    elif [ "$free_space" -gt 10 ] 2>/dev/null; then
        check_warn "${free_space}GB 可用（空间不足）"
        RESULTS["${node}_disk"]="warning"
        ISSUES+=("{\"node\":\"$node\",\"check\":\"disk\",\"severity\":\"warning\",\"message\":\"磁盘空间不足\",\"suggestion\":\"清理日志和缓存文件\"}")
    else
        check_fail "${free_space}GB 可用（严重不足）"
        RESULTS["${node}_disk"]="failed"
        ISSUES+=("{\"node\":\"$node\",\"check\":\"disk\",\"severity\":\"error\",\"message\":\"磁盘空间严重不足\",\"suggestion\":\"立即清理磁盘\"}")
    fi
done

echo ""

# ========== 生成报告 ==========
echo "=========================================="
echo "   检查总结"
echo "=========================================="

ERROR_COUNT=0
WARNING_COUNT=0
PASS_COUNT=0

for key in "${!RESULTS[@]}"; do
    case "${RESULTS[$key]}" in
        pass) PASS_COUNT=$((PASS_COUNT + 1)) ;;
        warning) WARNING_COUNT=$((WARNING_COUNT + 1)) ;;
        failed) ERROR_COUNT=$((ERROR_COUNT + 1)) ;;
    esac
done

echo "通过: $PASS_COUNT"
echo "警告: $WARNING_COUNT"
echo "失败: $ERROR_COUNT"
echo ""

if [ $ERROR_COUNT -eq 0 ] && [ $WARNING_COUNT -eq 0 ]; then
    OVERALL_STATUS="ready"
    echo "状态: ✓ 环境就绪，可以启动服务"
elif [ $ERROR_COUNT -eq 0 ]; then
    OVERALL_STATUS="warning"
    echo "状态: ⚠ 有警告但可以继续"
else
    OVERALL_STATUS="failed"
    echo "状态: ✗ 有严重问题，需要修复"
fi

echo ""

# ========== 输出问题列表 ==========
if [ ${#ISSUES[@]} -gt 0 ]; then
    echo "发现的问题:"
    echo ""
    for issue in "${ISSUES[@]}"; do
        node=$(echo "$issue" | jq -r '.node')
        check=$(echo "$issue" | jq -r '.check')
        severity=$(echo "$issue" | jq -r '.severity')
        message=$(echo "$issue" | jq -r '.message')
        suggestion=$(echo "$issue" | jq -r '.suggestion')

        echo "  [$node] $check"
        echo "    级别: $severity"
        echo "    问题: $message"
        echo "    建议: $suggestion"
        echo ""
    done
fi

# ========== 输出到文件 ==========
if [ -n "$OUTPUT" ]; then
    # 构造 JSON 报告
    issues_json="[$(IFS=,; echo "${ISSUES[*]}")]"

    cat > "$OUTPUT" << EOF
{
  "status": "$OVERALL_STATUS",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "summary": {
    "pass": $PASS_COUNT,
    "warning": $WARNING_COUNT,
    "failed": $ERROR_COUNT,
    "total": $((PASS_COUNT + WARNING_COUNT + ERROR_COUNT))
  },
  "issues": $issues_json
}
EOF

    echo "详细报告已保存到: $OUTPUT"
fi

# ========== 返回退出码 ==========
if [ "$OVERALL_STATUS" = "failed" ]; then
    exit 1
else
    exit 0
fi
