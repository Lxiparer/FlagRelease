#!/bin/bash
# 多节点容器启动工具
#
# 功能：
# 1. 在所有节点上启动同名容器
# 2. 挂载相同的模型路径和工作目录
# 3. 确保所有节点容器启动成功

set -euo pipefail

# ========== 参数解析 ==========
IMAGE=""
CONTAINER=""
MODEL_PATH=""
WORKSPACE_PATH=""
SSH_KEY=""
SSH_USER="root"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --image) IMAGE="$2"; shift 2 ;;
        --container) CONTAINER="$2"; shift 2 ;;
        --model-path) MODEL_PATH="$2"; shift 2 ;;
        --workspace-path) WORKSPACE_PATH="$2"; shift 2 ;;
        --ssh-key) SSH_KEY="$2"; shift 2 ;;
        --ssh-user) SSH_USER="$2"; shift 2 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

if [ -z "$IMAGE" ] || [ -z "$CONTAINER" ] || [ -z "$MODEL_PATH" ] || [ -z "$WORKSPACE_PATH" ]; then
    echo "用法: $0 --image <镜像> --container <容器名> --model-path <模型路径> --workspace-path <工作目录> [--ssh-key <密钥>] [--ssh-user <用户>]"
    exit 1
fi

# ========== 读取节点列表 ==========
# 从主节点容器读取 context.yaml
NODES_JSON=$(docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 -c \"
import yaml
ctx = yaml.safe_load(open('/flagos-workspace/shared/context.yaml'))
nodes = ctx.get('cluster', {}).get('nodes', [])
import json
print(json.dumps(nodes))
\"")

if [ -z "$NODES_JSON" ] || [ "$NODES_JSON" = "[]" ]; then
    echo "错误: 无法读取节点列表，请先运行 setup_ssh_cluster.sh"
    exit 1
fi

# 提取节点信息
NODES=($(echo "$NODES_JSON" | python3 -c "
import sys, json
nodes = json.load(sys.stdin)
for n in nodes:
    print(f\"{n['host']}:{n['rank']}\")
"))

echo "[launch_containers_multi] 启动多节点容器..."
echo "  镜像: ${IMAGE}"
echo "  容器名: ${CONTAINER}"
echo "  节点数: ${#NODES[@]}"

# ========== SSH 配置 ==========
if [ -n "$SSH_KEY" ]; then
    SSH_OPTS="-i ${SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=10"
else
    SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"
fi

# ========== 在所有节点启动容器 ==========
SUCCESS_COUNT=0
FAILED_NODES=()

for node_info in "${NODES[@]}"; do
    IFS=':' read -r host rank <<< "$node_info"

    echo "  [Rank ${rank}] 启动节点 ${host}..."

    # 检查容器是否已存在
    EXISTING=$(ssh ${SSH_OPTS} "${SSH_USER}@${host}" "docker ps -aq -f name=^${CONTAINER}$" 2>/dev/null || echo "")

    if [ -n "$EXISTING" ]; then
        # 检查镜像是否一致
        EXISTING_IMAGE=$(ssh ${SSH_OPTS} "${SSH_USER}@${host}" "docker inspect --format='{{.Config.Image}}' ${CONTAINER}" 2>/dev/null || echo "")

        if [ "$EXISTING_IMAGE" = "$IMAGE" ]; then
            echo "    ✓ 容器已存在且镜像一致，复用"
            SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
            continue
        else
            echo "    ⚠ 容器已存在但镜像不一致，删除旧容器"
            ssh ${SSH_OPTS} "${SSH_USER}@${host}" "docker rm -f ${CONTAINER}" &>/dev/null || true
        fi
    fi

    # 启动容器
    if ssh ${SSH_OPTS} "${SSH_USER}@${host}" "docker run -itd \
        --name=${CONTAINER} \
        --gpus=all \
        --network=host \
        --shm-size=10g \
        -v ${MODEL_PATH}:${MODEL_PATH} \
        -v ${WORKSPACE_PATH}:/flagos-workspace \
        ${IMAGE} bash" &>/dev/null; then
        echo "    ✓ 容器启动成功"
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    else
        echo "    ✗ 容器启动失败"
        FAILED_NODES+=("${host}")
    fi
done

# ========== 验证结果 ==========
if [ ${#FAILED_NODES[@]} -gt 0 ]; then
    echo ""
    echo "错误: 以下节点容器启动失败:"
    for node in "${FAILED_NODES[@]}"; do
        echo "  - ${node}"
    done
    exit 1
fi

echo ""
echo "============================================================"
echo "  多节点容器启动完成"
echo "============================================================"
echo "  成功: ${SUCCESS_COUNT}/${#NODES[@]} 个节点"
echo "============================================================"
