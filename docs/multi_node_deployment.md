# FlagOS 多机部署指南

> 权威文档。历史过程性文档已归档至 `docs/archive/`。
> 本方案唯一采用 `deploy_vllm.py`（配 `deploy_config.yaml`），第一代工具已废弃（见各 `_deprecated/` 目录）。

## 概述

FlagOS 使用 vLLM 原生分布式能力（TP + PP）在多节点部署大参数模型，**不使用 Ray**。
多机部署由独立工具 `skills/flagos-container-preparation/tools/deploy_vllm.py` 完成：

- 配置文件驱动（`deploy_config.yaml`），不侵入单机流程
- 内置 paramiko SSH（仅免密公钥认证，master 直连各子节点内网 IP）
- 自管容器生命周期：`--create-container` / `--pull-image` / `--load-image` / `--restart-container` / `--delete-container`
- 服务生命周期：deploy（默认）/ `--stop` / `--status` / `--clear-cache` / `--fetch-logs`
- 各节点并行执行，master 节点标记 `local: true` 时本地执行、无需 SSH 回环

**定位**：多机部署专用工具，仅在显式多机场景使用。不传多机参数时，单机流程完全不变。

---

## 已验证配置

> 2026-08-25 真实验证通过：

| 模型 | 配置 | 镜像 | 结果 |
|------|------|------|------|
| Qwen3.6-35B-A3B | TP=1 PP=2 nnodes=2 | `flagrelease_nvidia_vllm020plugin_base:0701` (vLLM 0.20.2) | ✅ 健康检查 + 推理通过 |
| Qwen3.6-27B | TP=1 PP=2 nnodes=2 | 同上 | ✅ 健康检查 + 推理通过 |

### 关键成功因素（务必遵守）

1. **worker 节点（node-rank>0）必须加 `--headless`** —— `deploy_vllm.py` 在 `node_rank>0` 时自动追加
2. 使用 vLLM 原生 CLI 参数：`--nnodes` / `--node-rank` / `--master-addr`
3. 所有节点模型路径必须完全一致
4. **不需要** `--distributed-executor-backend mp`（vLLM 自动处理）

> ⚠️ 历史勘误：早期报告曾归因"vLLM 0.20.2 multiproc executor 多节点不稳定"，
> 该结论**错误**。真实根因是第一代 `start_service_distributed.sh` 生成的 worker 命令
> **缺少 `--headless`**，导致 worker 误起 API server 与 master 争抢初始化，表现为
> `is_in_the_same_node: Connection closed by peer`。补上 `--headless` 后，同一 vLLM 0.20.2
> 镜像即可跑通。相关被证伪文档已删除。

---

## 前置条件

**网络**：所有节点同一内网（NCCL 通信）；master 可 SSH 免密直连各子节点内网 IP；开放服务端口（默认 8000）与 NCCL 端口（默认 29500）。

**环境一致性**：相同镜像、相同容器名、模型权重在相同路径、GPU 数量一致（推荐）。

**模型存储**：推荐 NFS/GPFS 共享挂载到统一路径；或各节点本地放置相同路径的副本。

---

## 快速开始

### 方式一：流水线多机模式（推荐）

在 master 节点上执行 `run_pipeline.sh`，附带多机参数即触发多机分支（步骤 1、3 走 `deploy_vllm.py`，步骤 2、4-13 复用主流程）：

```bash
bash prompts/run_pipeline.sh \
  harbor.baai.ac.cn/flagrelease-public/flagrelease_nvidia_vllm020plugin_base:0701 \
  Qwen3.6-27B \
  <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD> \
  --nnode 2 \
  --nodes "172.21.16.6:8,172.21.16.14:8" \
  --master-addr 172.21.16.6 \
  --ssh-key /root/.ssh/id_rsa
```

流水线会据此参数生成 `deploy_config.yaml`，然后:
1. `deploy_vllm.py --create-container` 在所有节点创建同名容器
2. 在 master 容器部署工具脚本（`setup_workspace.sh`）
3. 环境检测（master 节点，各节点环境一致）
4. `deploy_vllm.py` 起分布式服务 + 健康检查
5. 步骤 4-13 复用主流程，评测/性能仅请求 master 节点 `http://<master>:8000`

### 方式二：手动使用 deploy_vllm.py

直接编辑 `deploy_config.yaml` 后独立调用（适合调试）：

```bash
cd skills/flagos-container-preparation/tools
python3 deploy_vllm.py --config deploy_config.yaml --create-container   # 建容器
python3 deploy_vllm.py --config deploy_config.yaml                      # 部署+起服务+健康检查
python3 deploy_vllm.py --config deploy_config.yaml --status             # 查状态
python3 deploy_vllm.py --config deploy_config.yaml --fetch-logs --lines 200  # 拉日志
python3 deploy_vllm.py --config deploy_config.yaml --stop               # 停服务
```

---

## run_pipeline.sh 多机参数

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--nnode` | 是 | 1 | 节点总数（>1 触发多机模式）|
| `--nodes` | 是 | - | 节点列表 `<host>:<gpus>[,<host>:<gpus>]`，第一个为 master |
| `--master-addr` | 是 | - | 主节点 IP（NCCL 通信 + 对外 API）|
| `--master-port` | 否 | 29500 | NCCL 通信端口 |
| `--ssh-key` | 否 | - | SSH 私钥路径（免密时可省）|
| `--ssh-user` | 否 | root | SSH 用户名 |
| `--network-if` | 否 | eth0 | `NCCL_SOCKET_IFNAME` |

---

## deploy_config.yaml 结构

见 `skills/flagos-container-preparation/tools/deploy_config.yaml`（含完整注释）。核心字段：

```yaml
ssh:
  key_file: ""          # 留空用系统默认密钥 ~/.ssh/id_rsa / ssh-agent 免密
  port: 22
docker:
  container_name: "multi-test"   # 各节点容器名，必须一致
  image: "harbor.baai.ac.cn/.../flagrelease_nvidia_vllm020plugin_base:0701"
  run_args: >- --network=host --ipc=host --shm-size=64g --gpus='"device=0"' -v /data/models/X:/models:ro
nodes:                  # 按 node-rank 顺序，第一个为 master
  - {name: node-0, host: 172.21.16.6, ssh_user: root, local: true}
  - {name: node-1, host: 172.21.16.14, ssh_user: root}
vllm:
  model_path: /models
  served_model_name: qwen3.6-27b
  tensor_parallel_size: 1
  pipeline_parallel_size: 2
  max_model_len: 8192
  trust_remote_code: true
  enforce_eager: true
  extra_env: {VLLM_PLUGINS: fl, NCCL_SOCKET_IFNAME: eth0, GLOO_SOCKET_IFNAME: eth0}
```

**SSH 免密认证（唯一方式）**：master 直连各子节点内网 `host`，走系统默认密钥 `~/.ssh/id_rsa` / ssh-agent。`ssh_user` 通常为 `root`。前置条件：先用 `setup_passwordless_ssh.sh` 配好 master→worker 免密。

---

## 工作流集成（步骤透明切换）

| 步骤 | 单机 | 多机 |
|------|------|------|
| 1 容器准备 | 单容器 | 生成 config → `deploy_vllm.py --create-container` 全节点 |
| 2 环境检测 | 单节点 | master 节点（各节点环境一致）|
| 3 启动服务 | `start_service.sh` | `deploy_vllm.py`（deploy + health_check）|
| 4 精度评测 | master API | master API（无差异）|
| 5/7 算子调优 | 单节点调优 | 调优逻辑零改动，deploy_vllm.py 的 `sync_files` 自动同步算子禁用状态到所有节点 |
| 6 性能评测 | master API | master API（无差异）|
| 8 发布 | master 打包 | master 打包 |
| 9-13 Plugin | 单节点 | master 节点（复用算子集）|

---

## 算子调优（多机场景）

多机场景下算子调优流程与单机完全一致，调优决策逻辑零改动。

**机制**：
1. **算子禁用**：`toggle_flaggems.py --action modify-enable --disabled-ops` 在 **master 容器**写入 `/root/flaggems_ops_control.json`（算子白/黑名单）+ 持久化 `FLAGGEMS_CONTROL_MODE`、`USE_FLAGGEMS` 到 `/etc/environment`
2. **状态同步**：重启服务时，deploy_vllm.py 自动将这些文件从 master 复制到所有 worker 容器相同路径
3. **全节点生效**：各节点 vLLM 启动时读相同的算子控制状态，保证禁用一致

**配置**：`deploy_config.yaml` 模板已默认开启 `sync_files` 段（单节点时自动跳过，无副作用）：

```yaml
sync_files:
  - /root/flaggems_ops_control.json    # FlagGems 算子禁用列表
  - /etc/environment                   # 环境变量持久化（含 FLAGGEMS_CONTROL_MODE）
```

> `run_pipeline.sh` 生成 config 时也会写入此段，禁止省略——缺失会导致 master 禁用算子后 worker 仍用全量算子，全节点不一致。

**原理**：FlagGems 算子在每个节点的 worker 进程都会执行（每个节点都跑模型层），禁用状态必须全节点一致，否则 worker 用全量算子、master 用裁剪算子会导致不一致甚至崩溃。deploy_vllm.py 在启动前自动完成同步，对调优循环透明。

---

## TP/PP 选择

由 `deploy_config.yaml` 的 `tensor_parallel_size` / `pipeline_parallel_size` 显式指定。经验策略：

- **优先 TP**（单节点内并行，通信开销小）：单节点显存足够时 PP=1，TP=节点内 GPU 数
- **需要 PP**（跨节点流水线）：单节点显存不足时启用，PP≤节点数，TP 打满单节点
- TP/PP 均取 2 的幂；`TP × PP = world_size = 总 GPU 数`
- 典型：2 节点×8 卡 → TP=8, PP=2；4 节点×8 卡 → TP=8, PP=4

---

## 常见问题

**Q: worker 启动报 `is_in_the_same_node: Connection closed by peer`？**
A: worker 命令缺 `--headless`。deploy_vllm.py 已自动处理；若手动起服务务必对 node-rank>0 加 `--headless`。

**Q: `weights not initialized from checkpoint`（vllm_fl 插件 PP 场景）？**
A: 插件在跨节点 PP 时的权重分片问题。可禁用插件（`extra_env` 去掉 `VLLM_PLUGINS`）或用不带插件镜像验证。

**Q: 如何验证 SSH 连通性？**
A: `python3 deploy_vllm.py --config deploy_config.yaml --status`（含 preflight 检查）。

**Q: 如何切回单机？**
A: 不传 `--nnode` / `--nodes` 即可，单机流程完全不变。

**Q: 模型如何分发到各节点？**
A: 推荐 NFS/GPFS 共享挂载；或 `rsync -avz /data/models/X root@node:/data/models/`。

---

## 技术架构

```
        编排机 (运行 deploy_vllm.py)
           │ paramiko SSH（master 若 local:true 则本地执行）
     ┌─────┴─────┐
     ▼           ▼
┌─────────┐  ┌─────────┐
│ Master  │  │ Worker  │
│ rank 0  │  │ rank 1  │
│ API:8000│  │--headless│  ← 无 HTTP，纯 worker 参与计算
│ TP组    │  │ TP组     │
└────┬────┘  └────┬────┘
     └── NCCL/IB ──┘  (PP 跨节点流水线)
```

- **TP（Tensor Parallel）**：单节点内 GPU 间切分权重矩阵
- **PP（Pipeline Parallel）**：跨节点切分模型层
- **MP Backend**：vLLM 自动处理，无需显式 `--distributed-executor-backend`

---

## 参考资料

- [vLLM Distributed Serving](https://docs.vllm.ai/en/latest/serving/distributed_serving.html)
- [NCCL 环境变量](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)
- 历史文档：`docs/archive/MULTI_NODE_*.md`
