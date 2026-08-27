#!/bin/bash
# SSH 免密配置和节点探测工具
#
# 功能：
# 1. 解析节点列表（NODE_LIST="10.0.0.1:8,10.0.0.2:8"）
# 2. 检测 SSH 连通性
# 3. 分发 SSH 公钥（如未免密）
# 4. 验证节点间互联
# 5. 检测每个节点的 GPU 数量和型号
# 6. 验证所有节点模型路径可访问性
# 7. 写入 context.yaml 的 cluster.nodes[]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ========== 参数解析 ==========
NODE_LIST=""
SSH_KEY=""
SSH_USER="root"
MODEL_PATH=""
CONTAINER=""
NETWORK_IF=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --node-list) NODE_LIST="$2"; shift 2 ;;
        --ssh-key) SSH_KEY="$2"; shift 2 ;;
        --ssh-user) SSH_USER="$2"; shift 2 ;;
        --model-path) MODEL_PATH="$2"; shift 2 ;;
        --container) CONTAINER="$2"; shift 2 ;;
        --network-if) NETWORK_IF="$2"; shift 2 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

if [ -z "$NODE_LIST" ] || [ -z "$CONTAINER" ]; then
    echo "用法: $0 --node-list <节点列表> --container <容器名> [--ssh-key <密钥路径>] [--ssh-user <用户名>] [--model-path <模型路径>] [--network-if <网络接口>]"
    echo "示例: $0 --node-list '10.0.0.1:8,10.0.0.2:8' --container qwen3_671b --ssh-key /root/.ssh/id_rsa --model-path /data/models/Qwen3-671B"
    exit 1
fi

# ========== SSH 密钥检查 ==========
if [ -n "$SSH_KEY" ]; then
    if [ ! -f "$SSH_KEY" ]; then
        echo "错误: SSH 密钥文件不存在: ${SSH_KEY}"
        exit 1
    fi

    # 检查密钥权限
    KEY_PERM=$(stat -c "%a" "$SSH_KEY" 2>/dev/null || stat -f "%A" "$SSH_KEY" 2>/dev/null)
    if [ "$KEY_PERM" != "600" ] && [ "$KEY_PERM" != "400" ]; then
        echo "⚠ SSH 密钥权限不安全: ${KEY_PERM}，自动修正为 600"
        chmod 600 "$SSH_KEY"
    fi

    SSH_OPTS="-i ${SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=10"
else
    SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"
fi

# ========== 解析节点列表 ==========
echo "[setup_ssh_cluster] 解析节点列表..."
IFS=',' read -ra NODES <<< "$NODE_LIST"
NNODE=${#NODES[@]}

if [ "$NNODE" -lt 2 ]; then
    echo "错误: 多机模式至少需要 2 个节点，当前只有 ${NNODE} 个"
    exit 1
fi

echo "  节点总数: ${NNODE}"

# ========== 节点连通性检测 ==========
echo "[setup_ssh_cluster] 节点连通性检测..."
RANK=0
NODES_JSON="["
MASTER_ADDR=""

for node_spec in "${NODES[@]}"; do
    IFS=':' read -r host expected_gpus <<< "$node_spec"

    echo "  [Rank ${RANK}] ${host} (期望 ${expected_gpus} GPU)..."

    # SSH 连通性测试
    if ssh ${SSH_OPTS} "${SSH_USER}@${host}" "echo ok" &>/dev/null; then
        STATUS="reachable"
        echo "    ✓ SSH 连通"
    else
        echo "    ✗ SSH 连接失败，尝试配置免密..."

        # 尝试 ssh-copy-id
        if [ -n "$SSH_KEY" ]; then
            if ssh-copy-id -i "${SSH_KEY}.pub" "${SSH_USER}@${host}" &>/dev/null; then
                echo "    ✓ SSH 公钥已分发"
                STATUS="reachable"
            else
                echo "    ✗ SSH 公钥分发失败"
                STATUS="unreachable"
            fi
        else
            STATUS="unreachable"
        fi
    fi

    # 检测 GPU 数量
    if [ "$STATUS" = "reachable" ]; then
        GPU_COUNT=$(ssh ${SSH_OPTS} "${SSH_USER}@${host}" "nvidia-smi --query-gpu=count --format=csv,noheader | head -1" 2>/dev/null || echo "0")

        if [ "$GPU_COUNT" -eq 0 ]; then
            echo "    ⚠ GPU 检测失败，使用期望值 ${expected_gpus}"
            GPU_COUNT=$expected_gpus
        else
            echo "    ✓ 检测到 ${GPU_COUNT} 个 GPU"
        fi

        # 检测 GPU 型号
        GPU_TYPE=$(ssh ${SSH_OPTS} "${SSH_USER}@${host}" "nvidia-smi --query-gpu=name --format=csv,noheader | head -1" 2>/dev/null || echo "Unknown")
        echo "    GPU 型号: ${GPU_TYPE}"

        # 验证模型路径（如果提供）
        if [ -n "$MODEL_PATH" ]; then
            if ssh ${SSH_OPTS} "${SSH_USER}@${host}" "[ -d ${MODEL_PATH} ]" 2>/dev/null; then
                echo "    ✓ 模型路径存在: ${MODEL_PATH}"

                # 验证关键文件
                if ! ssh ${SSH_OPTS} "${SSH_USER}@${host}" "[ -f ${MODEL_PATH}/config.json ]" 2>/dev/null; then
                    echo "    ✗ 模型路径不完整，缺少 config.json"
                    STATUS="unreachable"
                fi
            else
                echo "    ✗ 模型路径不存在: ${MODEL_PATH}"
                STATUS="unreachable"
            fi
        fi
    else
        GPU_COUNT=$expected_gpus
        GPU_TYPE="Unknown"
    fi

    # 记录主节点地址
    if [ $RANK -eq 0 ]; then
        MASTER_ADDR="$host"
    fi

    # 构造节点 JSON
    if [ $RANK -gt 0 ]; then
        NODES_JSON="${NODES_JSON},"
    fi
    NODES_JSON="${NODES_JSON}{\"rank\":${RANK},\"host\":\"${host}\",\"ssh_port\":22,\"gpus\":${GPU_COUNT},\"container_name\":\"${CONTAINER}\",\"status\":\"${STATUS}\"}"

    if [ "$STATUS" = "unreachable" ]; then
        echo "错误: 节点 ${host} 不可达，流程终止"
        exit 1
    fi

    RANK=$((RANK + 1))
done

NODES_JSON="${NODES_JSON}]"

echo "  ✓ 所有节点连通性验证通过"

# ========== 节点间互联检测 ==========
echo "[setup_ssh_cluster] 节点间互联检测..."
for i in "${!NODES[@]}"; do
    IFS=':' read -r host_i gpus_i <<< "${NODES[$i]}"
    for j in "${!NODES[@]}"; do
        if [ $i -eq $j ]; then
            continue
        fi
        IFS=':' read -r host_j gpus_j <<< "${NODES[$j]}"

        if ! ssh ${SSH_OPTS} "${SSH_USER}@${host_i}" "ping -c 1 -W 2 ${host_j} &>/dev/null"; then
            echo "  ✗ 节点 ${host_i} 无法 ping 通 ${host_j}"
            exit 1
        fi
    done
done
echo "  ✓ 节点间互联正常"

# ========== 网络接口检测 ==========
if [ -n "$NETWORK_IF" ]; then
    echo "[setup_ssh_cluster] 验证网络接口: ${NETWORK_IF}..."
    for node_spec in "${NODES[@]}"; do
        IFS=':' read -r host gpus <<< "$node_spec"
        if ! ssh ${SSH_OPTS} "${SSH_USER}@${host}" "ip link show ${NETWORK_IF} &>/dev/null"; then
            echo "  ✗ 节点 ${host} 不存在网络接口 ${NETWORK_IF}"
            exit 1
        fi
    done
    echo "  ✓ 所有节点网络接口验证通过"
fi

# ========== 写入 context.yaml ==========
echo "[setup_ssh_cluster] 写入 context.yaml..."

# 使用 update_context.py 写入
docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/update_context.py \\
    --set cluster.mode=multi \\
    --set cluster.nnode=${NNODE} \\
    --json-set 'cluster.nodes=${NODES_JSON}' \\
    --set cluster.ssh_key_path='${SSH_KEY}' \\
    --set cluster.ssh_user='${SSH_USER}' \\
    --set cluster.network_interface='${NETWORK_IF}' \\
    --set distributed.master_addr='${MASTER_ADDR}' \\
    --json"

echo "  ✓ 配置已写入 context.yaml"

# ========== 输出摘要 ==========
echo ""
echo "============================================================"
echo "  SSH 集群配置完成"
echo "============================================================"
echo "  节点总数: ${NNODE}"
echo "  主节点: ${MASTER_ADDR}"
echo "  模型路径: ${MODEL_PATH:-未指定}"
echo "  网络接口: ${NETWORK_IF:-默认}"
echo "============================================================"
