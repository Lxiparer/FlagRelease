# 已废弃的第一代多机辅助工具

第一代多机方案的集群辅助脚本，已被 `deploy_vllm.py` 的对应子命令取代。

| 脚本 | 被取代方式 |
|------|-----------|
| `exec_on_nodes.sh` | `deploy_vllm.py`（各子命令并行 SSH 执行）|
| `verify_cluster_env.sh` | `deploy_vllm.py --status` + preflight_check |
| `diagnose_startup_failure.sh` | `deploy_vllm.py --fetch-logs` + `diagnose_ops.py`（依赖已归档的 exec_on_nodes.sh，读取第一代 startup_rank{N}.log）|

保留仅为 git 可追溯，请勿在流程中引用。
