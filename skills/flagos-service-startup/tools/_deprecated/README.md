# 已废弃的第一代多机工具

这些脚本是第一代多机方案（context.yaml 驱动、从 master 容器 SSH 外联）的产物，已被
`skills/flagos-container-preparation/tools/deploy_vllm.py`（deploy_config.yaml 驱动、
paramiko SSH、自管容器生命周期）取代。

| 脚本 | 被取代方式 |
|------|-----------|
| `start_service_distributed.sh` | `deploy_vllm.py`（deploy）|
| `calc_tp_pp.py` | 由 `deploy_config.yaml` 显式指定 TP/PP |
| `collect_multi_node_logs.sh` | `deploy_vllm.py --fetch-logs` |

**重要**：第一代 `start_service_distributed.sh` 生成的 worker 命令缺少 `--headless`，
这是此前 `MULTI_NODE_VERIFICATION_RESULT.md` 中"vLLM 0.20.2 multiproc 多节点不稳定"
误判的真实根因。`deploy_vllm.py` 在 node_rank>0 时自动追加 `--headless`，同一 vLLM 0.20.2
镜像上 Qwen3.6-27B / 35B-A3B（TP=1 PP=2 nnodes=2）已验证通过。

保留仅为 git 可追溯，请勿在流程中引用。
