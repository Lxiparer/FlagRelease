# FlagRelease — FlagOS 自动化发布框架

FlagOS 模型迁移与发布的全自动化工作流框架，基于 Claude Code 驱动，支持 NVIDIA 和华为昇腾平台。

## 功能概览

从容器准备到镜像发布的 13 步全自动流程，零人工交互：

| 步骤 | 名称 | 说明 |
|------|------|------|
| 1 | 容器准备 | 自动识别容器/镜像 + 模型权重搜索 + 工具部署 |
| 2 | 环境检测 | 场景分类（native / vllm_flaggems / vllm_plugin_flaggems） |
| 3 | 启服务 | V1(native) + V2(flagos) 启动验证 |
| 4 | 精度评测 | V1/V2 GPQA Diamond 对比 |
| 5 | 精度算子调优 | 条件触发，分组排查定位问题算子 |
| 6 | 性能评测 | V1/V2 benchmark 对比 |
| 7 | 性能算子调优 | 条件触发，逐个禁用直到达标 |
| 8 | 自动发布 | 打包 + Harbor 上传（合格公开/不合格私有） |
| 9-13 | Plugin 验证 | 安装 → 启服务 → 精度 → 性能 → 发布 |

## 项目结构

```
├── skills/                     # 各步骤的 Skill 定义和工具脚本
│   ├── flagos-container-preparation/
│   ├── flagos-pre-service-inspection/
│   ├── flagos-service-startup/
│   ├── flagos-performance-testing/
│   ├── flagos-operator-replacement/
│   ├── flagos-eval-comprehensive/
│   ├── flagos-component-install/
│   ├── flagos-plugin-install/
│   ├── flagos-release/
│   ├── flagos-issue-reporter/
│   ├── flagos-log-analyzer/
│   └── shared/
├── prompts/                    # 流水线启动脚本
│   ├── run_pipeline.sh         # 单模型流水线
│   └── run_batch.sh            # 批量执行
├── tools/
│   ├── notifications/          # 旁路进度汇报 + 飞书通知
│   └── batch_summarize/        # 批次/单模型结果分析
├── shared/                     # 共享工具（报告生成、context 更新等）
├── CLAUDE.md                   # Claude Code 项目指令
└── settings.local.json         # 权限预配置
```

## 快速开始

### 单机部署

第一参数自动识别：已有容器名 → 容器模式，否则视为镜像地址。7 个位置参数固定顺序为 `<容器名或镜像地址> <模型名> <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD>`。

```bash
# 单模型流水线（容器模式：第一参数为已有容器名）
bash prompts/run_pipeline.sh <container_name> <model_name> \
  <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD>

# 单模型流水线（镜像模式：第一参数为镜像地址）
bash prompts/run_pipeline.sh <image:tag> <model_name> \
  <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD>

# 批量执行（tasks.txt 每行 "目标 | 模型名"）
bash prompts/run_batch.sh tasks.txt \
  <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD>
```

常用可选参数（追加在位置参数之后）：

| 参数 | 适用 | 说明 |
|------|------|------|
| `--verbose` | 两者 | 显示全量终端输出（调试用） |
| `--proxy p1,p2,...` | 两者 | 代理列表，网络失败时自动切换 |
| `--datasets ds1,ds2,...` | 两者 | 精度评测数据集（gpqa_diamond/mmlu/math_500），默认 gpqa_diamond，每个独立评测与判定 |
| `--feishu-webhook URL` | 两者 | 飞书自定义机器人 Webhook，启用进度通知 |
| `--model-path <路径>` | 单模型 | 显式指定宿主机模型权重路径 |
| `--stop-on-error` | 批量 | 某任务失败即终止整个批次（默认继续下一个） |
| `--force` | 批量 | 强制重跑已完成任务（默认跳过 `all_done=true`） |
| `--timeout <秒>` | 批量 | 单模型超时，默认 86400（24 小时） |

### 多机部署

大参数模型（70B+）支持多节点分布式部署（TP + PP），无需 Ray。在 master 节点上执行，7 个位置参数后追加多机参数即触发多机分支（不传则完全走单机流程）：

```bash
# 单模型多机（2 节点 × 8 卡）
bash prompts/run_pipeline.sh \
  harbor.baai.ac.cn/flagrelease-public/qwen3-70b:latest \
  Qwen3-70B \
  <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD> \
  --nnode 2 \
  --nodes "10.0.0.1:8,10.0.0.2:8" \
  --master-addr 10.0.0.1 \
  --ssh-key ~/.ssh/id_rsa
```

批量任务固定在同一组机器上运行——多机参数在批次级一次性指定，全局透传给列表中每个任务（任务列表格式不变，仍是 `目标 | 模型名`）：

```bash
# 批量多机（tasks.txt 中所有模型复用同一组节点）
bash prompts/run_batch.sh \
  tasks.txt \
  <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD> \
  --nnode 2 \
  --nodes "10.0.0.1:8,10.0.0.2:8" \
  --master-addr 10.0.0.1 \
  --ssh-key ~/.ssh/id_rsa
```

多机参数说明：

| 参数 | 必填 | 说明 |
|------|------|------|
| `--nnode N` | 是 | 节点总数，>1 触发多机；不传则单机 |
| `--nodes "host:gpus,..."` | 是 | 节点列表，逗号分隔，第一个为 master |
| `--master-addr IP` | 是 | 主节点 IP（NCCL 通信 + 对外 API）|
| `--ssh-key path` | 否 | SSH 私钥路径（免密时可省）|
| `--ssh-user user` | 否 | SSH 用户名，默认 root |
| `--network-if iface` | 否 | NCCL/GLOO 网络接口，默认 eth0 |
| `--master-port port` | 否 | NCCL 通信端口，默认 29500 |

> `--nnode > 1` 时必须同时提供 `--nodes` 和 `--master-addr`，缺失会直接报错退出。

多机依赖 master 到各子节点的 SSH 免密。若尚未配置，可用免密配置脚本（经堡垒机分发 master 公钥、每子节点自动尝试 root/secure、幂等）先跑一次：

```bash
bash skills/flagos-container-preparation/tools/setup_passwordless_ssh.sh \
  --nodes "10.0.0.1:8,10.0.0.2:8" \
  --jump-user '<堡垒机用户>' --password '<堡垒机密码>' --master-ip 10.0.0.1
```

> 该脚本为流程外一次性手动配置，不属于自动化流水线；`-h` 查看全部参数。

**详细文档**：[多机部署指南](docs/multi_node_deployment.md)

## 飞书进度通知

汇报组件是完全旁路的观察者：不启动、不包装、不等待迁移流程。即使通知失败或组件缺失，迁移任务的执行、超时、清理和退出码都不受影响，最坏结果只是通知延迟或丢失。

通过 `--feishu-webhook` 传入 Webhook 地址即可启用（不传则不发送）：

```bash
# 批量执行 + 飞书通知
bash prompts/run_batch.sh tasks.txt \
    <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD> \
    --feishu-webhook 'https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx'

# 单模型同样支持
bash prompts/run_pipeline.sh <目标> <模型名> \
    <MODELSCOPE_TOKEN> <HF_TOKEN> <GITHUB_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD> \
    --feishu-webhook 'https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx'
```

批量场景只需在 `run_batch.sh` 传一次，Webhook 会自动继承到每个子流水线与后台汇报 worker，无需重复传递。飞书卡片包含任务开始、逐模型结果汇总、批次结束三类通知，展示达标上传数、成功率、模型耗时、迁移费用等指标。

> 注意：命令行参数会出现在 `ps` 进程列表中，Webhook 地址对同机其他用户可见。更多配置项（事件过滤、卡片行数、dry-run 等）见 `tools/notifications/README.md`。

## 支持平台

- NVIDIA GPU（推荐：vllm >= 0.7.3 + flaggems >= 5.1.0 + flagtree >= 0.5.0）
- 华为昇腾 NPU（vllm_ascend 适配层）

## 环境要求

- Claude Code CLI
- Docker
- 环境变量：`HARBOR_USER`、`HARBOR_PASSWORD`、`MODELSCOPE_TOKEN`、`HF_TOKEN`、`GITHUB_TOKEN`（按需）

## 分支说明

| 分支 | 用途 |
|------|------|
| main | 稳定版本 |
| huawei | 华为昇腾适配开发 |
| test | 集成测试 |
| hygon | 海光适配开发 |
| metax | 沐曦适配开发 |
