# 已废弃的第一代多机工具

已被 `deploy_vllm.py`（同目录）取代。

| 脚本 | 被取代方式 |
|------|-----------|
| `setup_ssh_cluster.sh` | `deploy_vllm.py` 内置 paramiko SSH（支持跳板机 user@role@ip）|
| `launch_containers_multi.sh` | `deploy_vllm.py --create-container` |

保留仅为 git 可追溯，请勿在流程中引用。
