#!/bin/bash
# 多节点分布式服务启动脚本
#
# 功能：
# 1. 从 context.yaml 读取集群配置和 TP/PP 参数
# 2. 在所有节点清理残留进程和缓存
# 3. 并行启动所有节点（rank 0 到 rank N-1）
# 4. 健康检查（主节点 HTTP + Worker 日志）
# 5. 成功后回写 context.yaml

set -euo pipefail

# ========== 参数解析 ==========
CONTAINER=""
MODE="default"  # default | flagos | native
PORT=8000

while [[ $# -gt 0 ]]; do
    case "$1" in
        --container) CONTAINER="$2"; shift 2 ;;
        --mode) MODE="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

if [ -z "$CONTAINER" ]; then
    echo "用法: $0 --container <容器名> [--mode default|flagos|native] [--port 8000]"
    exit 1
fi

echo "[start_service_distributed] 多节点服务启动..."

# ========== 读取配置 ==========
CTX_DATA=$(docker exec ${CONTAINER} cat /flagos-workspace/shared/context.yaml)

# 提取集群配置
NODES_JSON=$(echo "$CTX_DATA" | python3 -c "
import yaml, json, sys
ctx = yaml.safe_load(sys.stdin)
print(json.dumps(ctx.get('cluster', {}).get('nodes', [])))
")

NNODE=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('cluster', {}).get('nnode', 1))
")

MASTER_ADDR=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('distributed', {}).get('master_addr', ''))
")

MASTER_PORT=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('distributed', {}).get('master_port', 29500))
")

TP_SIZE=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('runtime', ).get('tp_size', 1))
")

PP_SIZE=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('distributed', {}).get('pp_size', 1))
")

WORLD_SIZE=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('distributed', {}).get('world_size', 1))
")

MODEL_PATH=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('model', {}).get('container_path', ''))
")

MODEL_NAME=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('model', {}).get('name', '').split('/')[-1] or 'default_model')
")

MAX_MODEL_LEN=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('service', {}).get('max_model_len', 32768))
")

NETWORK_IF=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('cluster', {}).get('network_interface', 'eth0'))
")

VISIBLE_DEVICES_ENV=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('gpu', {}).get('visible_devices_env', 'CUDA_VISIBLE_DEVICES'))
")

CUDA_VISIBLE=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('runtime', {}).get('cuda_visible_devices', ''))
")

SSH_KEY=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('cluster', {}).get('ssh_key_path', ''))
")

SSH_USER=$(echo "$CTX_DATA" | python3 -c "
import yaml, sys
ctx = yaml.safe_load(sys.stdin)
print(ctx.get('cluster', {}).get('ssh_user', 'root'))
")

if [ -z "$MASTER_ADDR" ] || [ "$NNODE" -lt 2 ]; then
    echo "错误: 配置不完整或不是多机模式 (nnode=${NNODE})"
    exit 1
fi

echo "  节点数: ${NNODE}"
echo "  主节点: ${MASTER_ADDR}:${MASTER_PORT}"
echo "  TP=${TP_SIZE}, PP=${PP_SIZE}, world_size=${WORLD_SIZE}"
echo "  模型: ${MODEL_PATH}"
echo "  模式: ${MODE}"

# ========== SSH 配置 ==========
if [ -n "$SSH_KEY" ]; then
    SSH_OPTS="-i ${SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=10"
else
    SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"
fi

# ========== 解析节点列表 ==========
NODES=($(echo "$NODES_JSON" | python3 -c "
import sys, json
nodes = json.load(sys.stdin)
for n in sorted(nodes, key=lambda x: x['rank']):
    print(f\"{n['host']}:{n['rank']}\")
"))

# ========== 清理所有节点残留进程和缓存 ==========
echo "[start_service_distributed] 清理残留进程和缓存..."
for node_info in "${NODES[@]}"; do
    IFS=':' read -r host rank <<< "$node_info"
    echo "  [Rank ${rank}] 清理节点 ${host}..."

    ssh ${SSH_OPTS} "${SSH_USER}@${host}" "docker exec ${CONTAINER} bash -c '
        pkill -9 -f vllm.entrypoints || true
        pkill -9 -f sglang.launch_server || true
        rm -rf /root/.triton/cache/ /tmp/triton_cache/ /root/.flaggems/code_cache/ || true
    '" &>/dev/null || echo "    ⚠ 清理失败（忽略）"
done
echo "  ✓ 清理完成"

# ========== 加载 FlagGems 配置（如果需要）==========
FLAGGEMS_ENV=""
if [ "$MODE" = "flagos" ]; then
    echo "[start_service_distributed] 加载 FlagGems 配置..."
    FLAGGEMS_ENV="[ -f /etc/environment ] && source /etc/environment;"
fi

# ========== 并行启动所有节点 ==========
echo "[start_service_distributed] 启动所有节点服务..."

echo "  model=${MODEL_PATH}"
echo "  served_model_name=${MODEL_NAME}"
echo "  nnodes=${NNODE}, node_rank=0..$((NNODE-1)), master=${MASTER_ADDR}:${MASTER_PORT}"
echo "  tp=${TP_SIZE}, pp=${PP_SIZE}"

for node_info in "${NODES[@]}"; do
    IFS=':' read -r host rank <<< "$node_info"

    # 每个节点按 rank 构建 vLLM 命令（vLLM 原生多节点 CLI 参数）
    # 关键: worker 节点 (node-rank>0) 必须加 --headless
    #   --headless 是 vLLM 多节点部署的标准必需参数:
    #   - rank 0 节点启动 API server (对外提供 HTTP 服务)
    #   - 非 rank 0 节点加 --headless 只做计算，不启动 API server
    #   - 不加会导致 worker 尝试启动 API 导致初始化混乱
    VLLM_CMD="vllm serve '${MODEL_PATH}' \\
        --host 0.0.0.0 \\
        --port ${PORT} \\
        --served-model-name '${MODEL_NAME}' \\
        --tensor-parallel-size ${TP_SIZE} \\
        --pipeline-parallel-size ${PP_SIZE} \\
        --max-model-len ${MAX_MODEL_LEN} \\
        --nnodes ${NNODE} \\
        --node-rank ${rank} \\
        --master-addr ${MASTER_ADDR} \\
        --master-port ${MASTER_PORT} \\
        --trust-remote-code"

    # worker 节点 (node-rank>0) 必须加 --headless
    if [ "${rank}" -gt 0 ]; then
        VLLM_CMD="${VLLM_CMD} \\
        --headless"
    fi

    (
        echo "  [Rank ${rank}] 启动节点 ${host}..."

        ssh ${SSH_OPTS} "${SSH_USER}@${host}" "docker exec ${CONTAINER} bash -c '
            export NCCL_SOCKET_IFNAME=${NETWORK_IF}
            export ${VISIBLE_DEVICES_ENV}=${CUDA_VISIBLE}
            export PATH=/opt/conda/bin:\$PATH

            ${FLAGGEMS_ENV}

            mkdir -p /flagos-workspace/logs

            nohup ${VLLM_CMD} \\
                > /flagos-workspace/logs/startup_rank${rank}.log 2>&1 &

            echo \$! > /flagos-workspace/logs/service_rank${rank}.pid
            echo \"Rank ${rank} 启动完成，PID: \$(cat /flagos-workspace/logs/service_rank${rank}.pid)\"
        '" || echo "  ✗ Rank ${rank} 启动失败"
    ) &
done

# 等待所有后台任务完成
wait
echo "  ✓ 所有节点启动命令已发出"

# ========== 健康检查 ==========
echo "[start_service_distributed] 健康检查..."

# 等待服务初始化
sleep 30

# 检查主节点 HTTP API
echo "  检查主节点 API (${MASTER_ADDR}:${PORT}/health)..."
MAX_RETRIES=20
RETRY=0
API_OK=false

while [ $RETRY -lt $MAX_RETRIES ]; do
    if curl -s --max-time 5 "http://${MASTER_ADDR}:${PORT}/health" &>/dev/null; then
        echo "  ✓ 主节点 API 响应正常"
        API_OK=true
        break
    fi
    RETRY=$((RETRY + 1))
    sleep 10
done

if [ "$API_OK" = "false" ]; then
    echo "  ✗ 主节点 API 健康检查超时"
    # 回传日志
    bash $(dirname $0)/collect_multi_node_logs.sh --container ${CONTAINER}
    docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/update_context.py --set workflow.service_ok=false --json"
    exit 1
fi

# 检查 Worker 节点日志关键词
echo "  检查 Worker 节点日志..."
WORKER_OK=true

for node_info in "${NODES[@]}"; do
    IFS=':' read -r host rank <<< "$node_info"

    if [ $rank -eq 0 ]; then
        continue  # 跳过主节点
    fi

    echo "    [Rank ${rank}] ${host}..."

    # 检查日志中是否出现成功关键词
    if ssh ${SSH_OPTS} "${SSH_USER}@${host}" "docker exec ${CONTAINER} bash -c 'timeout 60 bash -c \"
        while true; do
            if grep -q \"Multiprocess load balancer started\" /flagos-workspace/logs/startup_rank${rank}.log 2>/dev/null; then
                exit 0
            fi
            sleep 2
        done
    \"'" &>/dev/null; then
        echo "      ✓ Worker 启动成功"
    else
        echo "      ✗ Worker 启动超时或失败"
        WORKER_OK=false
    fi
done

if [ "$WORKER_OK" = "false" ]; then
    echo "  ✗ Worker 节点健康检查失败"
    # 回传日志
    bash $(dirname $0)/collect_multi_node_logs.sh --container ${CONTAINER}
    docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/update_context.py --set workflow.service_ok=false --json"
    exit 1
fi

# ========== 成功，回写 context ==========
echo "[start_service_distributed] 服务启动成功，更新 context..."
docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/update_context.py \\
    --set workflow.service_ok=true \\
    --set distributed.enabled=true \\
    --json"

echo ""
echo "============================================================"
echo "  多节点服务启动完成"
echo "============================================================"
echo "  主节点 API: http://${MASTER_ADDR}:${PORT}"
echo "  TP=${TP_SIZE}, PP=${PP_SIZE}, world_size=${WORLD_SIZE}"
echo "============================================================"
