#!/bin/bash
# FlagOS 多机部署功能测试脚本
# 用途：验证多机部署核心工具 deploy_vllm.py 的正确性（不启动实际容器）
#
# 多机唯一方案 = deploy_vllm.py + deploy_config.yaml。
# 第一代工具（start_service_distributed.sh/calc_tp_pp.py/setup_ssh_cluster.sh/
# launch_containers_multi.sh/collect_multi_node_logs.sh）已废弃，移入 _deprecated/。

set -e

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
TOOLS_DIR="$PROJECT_ROOT/skills/flagos-container-preparation/tools"

echo "=================================================="
echo "FlagOS 多机部署功能测试 (deploy_vllm.py)"
echo "=================================================="

# 测试 1: deploy_vllm.py 命令构造 + --headless 门控（关键：worker 必须带 --headless）
echo ""
echo "[测试 1] vLLM 命令构造与 --headless 门控"
echo "--------------------------------------------------"

python3 - "$TOOLS_DIR" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
import deploy_vllm

cfg = {
    "docker": {"container_name": "multi-test"},
    "nodes": [{"name": "node-0", "host": "172.21.16.6", "local": True},
              {"name": "node-1", "host": "172.21.16.14"}],
    "vllm": {"model_path": "/models", "served_model_name": "m",
             "host": "0.0.0.0", "port": 8000,
             "tensor_parallel_size": 1, "pipeline_parallel_size": 2,
             "max_model_len": 8192, "trust_remote_code": True,
             "enforce_eager": True,
             "extra_env": {"VLLM_PLUGINS": "fl", "NCCL_SOCKET_IFNAME": "eth0"}},
}

fails = 0
def check(cond, msg):
    global fails
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        fails += 1

master = deploy_vllm.build_vllm_command(cfg, 0)
worker = deploy_vllm.build_vllm_command(cfg, 1)

check("--headless" not in master, "master (node-rank=0) 不含 --headless")
check("--headless" in worker, "worker (node-rank=1) 含 --headless（历史失败根因）")
check("--nnodes 2 --node-rank 0" in master, "master 节点 --nnodes 2 --node-rank 0")
check("--nnodes 2 --node-rank 1" in worker, "worker 节点 --nnodes 2 --node-rank 1")
check("--master-addr 172.21.16.6" in master, "--master-addr 取 nodes[0].host")
check("--tensor-parallel-size 1" in master, "--tensor-parallel-size 透传")
check("--pipeline-parallel-size 2" in master, "--pipeline-parallel-size 透传")
check("--distributed-executor-backend" not in master, "不含 --distributed-executor-backend（vLLM 自动处理）")
check("--enforce-eager" in master, "--enforce-eager 透传")

sys.exit(1 if fails else 0)
PYEOF

# 测试 2: deploy_config.yaml 模板结构
echo ""
echo "[测试 2] deploy_config.yaml 模板结构"
echo "--------------------------------------------------"

python3 - "$TOOLS_DIR/deploy_config.yaml" <<'PYEOF'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1]))
fails = 0
def check(cond, msg):
    global fails
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        fails += 1

check("docker" in cfg and "container_name" in cfg["docker"], "docker.container_name 存在")
check("image" in cfg["docker"], "docker.image 存在")
check("nodes" in cfg and len(cfg["nodes"]) >= 1, "nodes[] 存在")
check(cfg["nodes"][0].get("local") is True, "nodes[0] 标 local: true（master 本地执行）")
v = cfg.get("vllm", {})
for k in ("model_path", "served_model_name", "tensor_parallel_size", "pipeline_parallel_size"):
    check(k in v, f"vllm.{k} 存在")
sf = cfg.get("sync_files") or []
check("/root/flaggems_ops_control.json" in sf, "sync_files 默认含 flaggems_ops_control.json（多机算子调优必需）")
check("/etc/environment" in sf, "sync_files 默认含 /etc/environment")
sys.exit(1 if fails else 0)
PYEOF

# 测试 3: 节点列表解析（run_pipeline.sh --nodes 格式 host:gpus,...）
echo ""
echo "[测试 3] 节点列表解析"
echo "--------------------------------------------------"

test_node_parse() {
    local nodes="$1"
    local expected_count=$2
    IFS=',' read -ra NODE_ARRAY <<< "$nodes"
    local count=${#NODE_ARRAY[@]}
    if [[ $count -eq $expected_count ]]; then
        echo "✓ 解析 '$nodes' → $count 个节点"
    else
        echo "✗ 解析 '$nodes' → 期望 $expected_count 个，实际 $count 个"
        exit 1
    fi
}

test_node_parse "172.21.16.6:8,172.21.16.14:8" 2
test_node_parse "192.168.1.1:8,192.168.1.2:8,192.168.1.3:8,192.168.1.4:8" 4

# 测试 4: run_pipeline.sh 多机参数支持
echo ""
echo "[测试 4] Pipeline 脚本参数支持"
echo "--------------------------------------------------"

check_param() {
    if grep -qe "$1" "$PROJECT_ROOT/prompts/run_pipeline.sh"; then
        echo "✓ $2 支持"
    else
        echo "✗ $2 缺失"; exit 1
    fi
}
check_param "\-\-nnode" "--nnode 参数"
check_param "\-\-nodes" "--nodes 参数"
check_param "\-\-master-addr" "--master-addr 参数"
check_param "master_addr=\${MASTER_ADDR}" "MASTER_ADDR 透传到 MULTINODE_PARAMS"
check_param "deploy_vllm.py" "多机分支引用 deploy_vllm.py"

# 测试 4b: run_batch.sh 批量多机参数透传
echo ""
echo "[测试 4b] Batch 脚本多机参数透传"
echo "--------------------------------------------------"

check_batch() {
    if grep -qe "$1" "$PROJECT_ROOT/prompts/run_batch.sh"; then
        echo "✓ $2 支持"
    else
        echo "✗ $2 缺失"; exit 1
    fi
}
check_batch "MULTINODE_FLAGS" "MULTINODE_FLAGS 数组收集多机参数"
check_batch "\-\-nnode|\-\-nodes|\-\-node-list|\-\-master-addr" "批量解析多机参数"
check_batch 'MULTINODE_FLAGS\[@\]+"\${MULTINODE_FLAGS\[@\]}"' "多机参数透传给 run_pipeline.sh"
check_batch "必须同时提供 \-\-nodes 和 \-\-master-addr" "多机参数完整性校验"

# 语法自检
if bash -n "$PROJECT_ROOT/prompts/run_batch.sh"; then
    echo "✓ run_batch.sh 语法正确"
else
    echo "✗ run_batch.sh 语法错误"; exit 1
fi

# 测试 5: 工具脚本存在性（deploy_vllm.py 在位，第一代已归档）
echo ""
echo "[测试 5] 工具脚本完整性"
echo "--------------------------------------------------"

check_exists() {
    if [[ -f "$PROJECT_ROOT/$1" ]]; then echo "✓ $1"; else echo "✗ $1 不存在"; exit 1; fi
}
check_absent() {
    if [[ ! -f "$PROJECT_ROOT/$1" ]]; then echo "✓ 已归档: $1"; else echo "✗ $1 仍在原位（应移入 _deprecated/）"; exit 1; fi
}

check_exists "skills/flagos-container-preparation/tools/deploy_vllm.py"
check_exists "skills/flagos-container-preparation/tools/deploy_config.yaml"
check_exists "skills/flagos-service-startup/tools/calc_tp_size.py"
# 第一代工具应已归档
check_absent "skills/flagos-service-startup/tools/start_service_distributed.sh"
check_absent "skills/flagos-service-startup/tools/calc_tp_pp.py"
check_absent "skills/flagos-service-startup/tools/collect_multi_node_logs.sh"
check_absent "skills/flagos-container-preparation/tools/setup_ssh_cluster.sh"
check_absent "skills/flagos-container-preparation/tools/launch_containers_multi.sh"

# 测试 6: setup_workspace.sh 部署清单（含 deploy_vllm.py，不含第一代）
echo ""
echo "[测试 6] Workspace 脚本工具部署清单"
echo "--------------------------------------------------"

SW="$PROJECT_ROOT/skills/flagos-container-preparation/tools/setup_workspace.sh"
if grep -q "deploy_vllm.py" "$SW"; then echo "✓ deploy_vllm.py 已加入部署清单"; else echo "✗ deploy_vllm.py 未加入部署清单"; exit 1; fi
if grep -q "deploy_config.yaml" "$SW"; then echo "✓ deploy_config.yaml 已加入部署清单"; else echo "✗ deploy_config.yaml 未加入部署清单"; exit 1; fi
if ! grep -q "start_service_distributed.sh" "$SW"; then echo "✓ 第一代 start_service_distributed.sh 已从清单移除"; else echo "✗ 清单仍引用已废弃的 start_service_distributed.sh"; exit 1; fi
if ! grep -q "calc_tp_pp.py" "$SW"; then echo "✓ 第一代 calc_tp_pp.py 已从清单移除"; else echo "✗ 清单仍引用已废弃的 calc_tp_pp.py"; exit 1; fi

echo ""
echo "=================================================="
echo "✓ 所有测试通过"
echo "=================================================="
