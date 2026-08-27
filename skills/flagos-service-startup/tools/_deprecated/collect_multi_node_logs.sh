#!/bin/bash
# 多节点日志回传工具
#
# 功能：
# 1. 从所有节点收集服务启动日志
# 2. 聚合到主节点容器的 /flagos-workspace/logs/
# 3. 用于崩溃诊断

set -euo pipefail

# ========== 参数解析 ==========
CONTAINER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --container) CONTAINER="$2"; shift 2 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

if [ -z "$CONTAINER" ]; then
    echo "用法: $0 --container <容器名>"
    exit 1
fi

echo "[collect_multi_node_logs] 收集多节点日志..."

# ========== 读取节点列表 ==========
NODES_JSON=$(docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 -c \"
import yaml, json
ctx = yaml.safe_load(open('/flagos-workspace/shared/context.yaml'))
print(json.dumps(ctx.get('cluster', {}).get('nodes', [])))
\"")

SSH_KEY=$(docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 -c \"
import yaml
ctx = yaml.safe_load(open('/flagos-workspace/shared/context.yaml'))
print(ctx.get('cluster', {}).get('ssh_key_path', ''))
\"")

SSH_USER=$(docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 -c \"
import yaml
ctx = yaml.safe_load(open('/flagos-workspace/shared/context.yaml'))
print(ctx.get('cluster', {}).get('ssh_user', 'root'))
\"")

if [ -z "$NODES_JSON" ] || [ "$NODES_JSON" = "[]" ]; then
    echo "错误: 无法读取节点列表"
    exit 1
fi

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
for n in nodes:
    print(f\"{n['host']}:{n['rank']}\")
"))

# ========== 收集日志 ==========
for node_info in "${NODES[@]}"; do
    IFS=':' read -r host rank <<< "$node_info"

    echo "  [Rank ${rank}] 收集节点 ${host} 日志..."

    # 从远端节点容器拷贝日志到本地临时目录
    LOG_FILE="/tmp/startup_rank${rank}.log"

    if ssh ${SSH_OPTS} "${SSH_USER}@${host}" "docker cp ${CONTAINER}:/flagos-workspace/logs/startup_rank${rank}.log ${LOG_FILE}" &>/dev/null; then
        # 从远端节点下载到宿主机临时目录
        scp ${SSH_OPTS} "${SSH_USER}@${host}:${LOG_FILE}" "/tmp/startup_rank${rank}.log" &>/dev/null

        # 拷贝到主节点容器
        docker cp "/tmp/startup_rank${rank}.log" "${CONTAINER}:/flagos-workspace/logs/" &>/dev/null

        # 清理临时文件
        rm -f "/tmp/startup_rank${rank}.log"
        ssh ${SSH_OPTS} "${SSH_USER}@${host}" "rm -f ${LOG_FILE}" &>/dev/null

        echo "    ✓ 日志已收集"
    else
        echo "    ⚠ 日志文件不存在或收集失败"
    fi
done

echo "  ✓ 日志收集完成，保存在主节点容器 /flagos-workspace/logs/"
