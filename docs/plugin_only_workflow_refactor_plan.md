# FlagOS Plugin-only 工作流完整重构方案

> 状态：待实施  
> 适用分支：`workflow-refactor`  
> 业务流程依据：[`plugin_only_workflow_optimization.md`](plugin_only_workflow_optimization.md)  
> 更新时间：2026-09-01

## 1. 目标与边界

本方案将已确认的 Plugin-only V3/V4 业务流程转换为可分段实施、独立验收和安全回滚的工程计划。

目标主链路：

```text
完整 FlagOS 组件准入
  → V3 全组件发现启动
  → V3 启动兼容性算子调优
  → V3 外部 NV 精度评测与调优
  → V3 绝对性能测量
  → V3 冻结与发布
  → V4 性能搜索
  → V4 精度回溯与条件发布
```

本轮只接受已经安装以下组件的镜像：

```text
vLLM + FlagGems + FlagTree + vllm-plugin-FL
```

目标运行环境固定为：

```bash
VLLM_PLUGINS=fl
USE_FLAGGEMS=1
```

### 本轮不实现

- 纯 vLLM V0 前置测试；
- 自动安装 FlagGems、FlagTree 或 Plugin；
- `gems+tree` 分支；
- Native/V1 本地精度或性能基线；
- V1.1/V1.2/V1.3 三选；
- 合成 Native 性能基线；
- V3 性能比较、性能 Gate 和性能算子调优；
- 通过关闭整个 FlagGems、FlagTree 或 Plugin 逼近原生环境。

未来 V0 应作为独立前置工作流接入，不应重新污染当前 V3/V4 主链路。

---

## 2. 重构原则

1. **先建新契约，再接新流程，最后删旧代码。**
2. **确定性行为归 Workflow Engine。** 状态推进、执行、验证、Artifact、Gate、revision 和发布决定均由本地代码负责。
3. **复杂分析收敛到可替换的 Analysis Agent。** 本轮以 Claude Code 为第一代运行时，后续可替换为 LangGraph + 任意兼容模型 Provider。
4. **Agent 只提假设，不制造事实。** 所有建议统一走 suggest–verify–commit，未经实测不得提交 revision 或改变业务结论。
5. **Artifact 是事实，Gate 是结论。** 不从文件名、Agent 文本或任意布尔值推导资格。
6. **执行状态与业务资格分离。** 命令成功不等于版本达标。
7. **外部 NV 精度是唯一业务红线。** 缺失或不兼容时 fail-closed。
8. **V3 性能只测量。** 性能高低不影响 V3 成立或流程推进。
9. **算子配置不可变。** 每次调优和验证实验创建新 revision，不覆盖父 revision。
10. **运行时 oplist 是实际生效证据。** 配置白名单只是控制输入。
11. **V4 不修改 V3。** V4 从冻结的 `v3-final` 克隆，失败时回退 V3。
12. **所有长任务与 Agent 会话可恢复、可审计。** 长任务继续使用 `task_runner.py` detached 协议，Agent 状态只保存结构化引用和 checkpoint。

---

## 3. 目标架构

### 3.1 分层

```text
入口层
  prompts/run_pipeline.sh
  prompts/run_batch.sh
        ↓
确定性 Workflow Engine
  graph / state transition / recovery / step runner
        ├───────────────┐
        ↓               ↓
领域执行器          AnalysisAgent
  admission           ClaudeCodeAnalysisAgent（本轮）
  startup             LangGraphAnalysisAgent（未来）
  operator                  ↓
  accuracy            结构化假设与受限实验建议
  performance               ↓
  release          Schema + Policy 校验后回到 Engine
        ↓
证据层
  Artifact registry / Gate reducer / operator revisions
        ↓
呈现层
  trace / ledger / timing / report / batch / notifications
```

Analysis Agent 是 Workflow Engine 调用的嵌套分析能力，不是额外 workflow step，也不拥有确定性状态转换。

### 3.2 Shell 与 Python 职责

`prompts/run_pipeline.sh` 最终收敛为薄入口，只负责：

- 参数解析和基础环境变量导入；
- 定位项目目录；
- 启动确定性 Python workflow engine；
- 转发退出码；
- 保留进程外日志过滤。

以下逻辑不应继续散落在 shell 条件分支中：

- A/B/native 分支选择；
- V1/V2/V3/Plugin 阶段跳转；
- Gate 判定；
- Artifact 文件猜测；
- 恢复点推导；
- 发布资格推导；
- 直接设置业务成功布尔值。

### 3.3 Analysis Agent 职责边界

确定性 Workflow Engine 负责状态转换、Artifact 校验、Gate 归约、revision 构建、服务重启、oplist 核验、评测解析、发布决策和恢复。复杂、开放式且难以完全编码的归因工作通过统一 `AnalysisAgent` 接口执行。

本轮运行架构：

```text
Deterministic Workflow Engine
    ├── 确定性执行、验证、恢复和发布
    ├── Artifact Registry / Gate Reducer / Revision Store
    └── AnalysisAgent
          └── ClaudeCodeAnalysisAgent（本轮主实现）
```

Claude Code 是第一代 **Analysis Agent Runtime**，不是状态机或业务裁决者。Workflow Engine 可在以下节点调用它：

- 启动崩溃的复杂跨日志归因；
- 确定性精度搜索未在预算内收敛后的回归分析；
- 证据不足、互相矛盾或未知类型的运行故障；
- 候选问题算子排序和受限验证实验建议。

任何 Analysis Agent 均不得：

- 直接修改当前 operator revision 或永久禁用算子；
- 写入 `accuracy.qualified`、`v3.established`、`v4.established`；
- 校验、修复或绕过外部 NV reference；
- 修改 Artifact 哈希、identity 或有效性；
- 创建 V3 performance qualification；
- 根据自然语言结论选择 V3/V4 发布版本。

所有建议统一执行：

```text
structured request
  → Agent hypothesis
  → schema validation
  → local policy validation
  → experimental child revision
  → deterministic verification
  → measured Artifact
  → reducer decision
  → commit or rollback with negative evidence
```

Agent 结论仅形成 `analysis_result` Artifact；只有验证实验产生的实测 Artifact 才能推动 revision、Gate 或状态转换。

### 3.4 可替换运行时与未来 LangGraph 边界

Workflow Engine 只能依赖 provider-neutral `AnalysisAgent` 接口，不直接依赖 Claude Code、LangGraph 或某个模型 API：

```python
class AnalysisAgent:
    def analyze_startup_failure(self, request: StartupFailureRequest) -> AnalysisResult:
        ...

    def analyze_accuracy_regression(self, request: AccuracyRegressionRequest) -> AnalysisResult:
        ...

    def analyze_unknown_failure(self, request: UnknownFailureRequest) -> AnalysisResult:
        ...
```

本轮实现 `ClaudeCodeAnalysisAgent`。后续独立阶段实现 `LangGraphAnalysisAgent` 和 `ModelProvider` 适配层。LangGraph 只替换分析 harness，不替换本文 15 步确定性 Workflow Engine。

```text
AnalysisAgent
  ├── ClaudeCodeAnalysisAgent（当前/回退）
  └── LangGraphAnalysisAgent（未来）
        └── ModelProvider
              ├── AnthropicProvider
              ├── OpenAIProvider
              ├── OpenAICompatibleProvider
              ├── InternalGatewayProvider
              └── LocalModelProvider
```

未来模型接入必须声明 structured output、tool calling、上下文长度、超时和工具轮数等能力。能力不足时只能降级到受校验文本 JSON、suggestion-only、其他 provider 回退或 `unresolved`，不能假设所有模型能力等价。

Agent 默认只获得受限领域工具，不获得不受控 shell，例如：

```text
read_service_log
read_runtime_oplist
read_operator_revision
run_diagnose_ops
propose_operator_revision
start_revision_verification
read_verification_result
create_issue_draft
```

工具策略必须执行路径限制、run/revision/Artifact identity 校验、输出限额、敏感信息脱敏、审计记录和读写权限分离。

---

## 4. 核心契约

### 4.1 Context Schema v2

运行时状态仍位于：

```text
/flagos-workspace/shared/context.yaml
```

`shared/context.template.yaml` 只用于初始化，禁止作为运行时状态直接读写。

建议顶层结构：

```yaml
schema_version: "2.0"
workflow_profile: plugin_only

run:
  id: ""
  status: pending
  started_at: ""
  finished_at: ""

entry: {}
model: {}
image: {}
container: {}
gpu: {}
workspace: {}
network: {}

admission:
  status: pending
  profile: plugin_only
  components: {}
  evidence_artifact: ""
  rejection_reason: ""

references:
  accuracy:
    source: external_nv
    manifest_artifact: ""
    max_relative_drop: 0.05

candidates:
  v3:
    status: pending
    discovered_revision: ""
    startup_stable_revision: ""
    final_revision: ""
    service: {}
    accuracy: {}
    performance: {}
    established: false
    establishment_gate: ""
    release: {}
  v4:
    status: pending
    base_candidate: v3
    base_revision: ""
    final_revision: ""
    performance: {}
    accuracy: {}
    execution_success: false
    established: false
    fallback_to_v3: false
    establishment_gate: ""
    release: {}

operator_revisions: {}
steps: {}
jobs: {}
agent_sessions:
  agent-session-001:
    task_type: startup_diagnosis
    workflow_run_id: run-001
    candidate: v3
    operator_revision: v3-startup-r2
    status: pending
    runtime: claude_code
    model_provider: claude_code
    checkpoint_id: ""
    input_artifacts: []
    output_artifacts: []
artifacts: []
gates: {}
issues: []
timing: {}
workflow_ledger: {}
metadata: {}
```

应移除的混合旧语义：

```text
workflow.accuracy_ok
workflow.performance_ok
workflow.qualified
plugin_workflow.*
baseline.v1_*
native_perf
flagos_full_perf
flagos_optimized_perf
```

执行状态和业务资格必须分开：

```yaml
steps:
  v3_final_accuracy:
    execution_status: succeeded

gates:
  v3_accuracy:
    qualification_status: failed
    qualified: false
```

### 4.2 Artifact Manifest

所有 Gate 输入必须先登记为 Artifact，不能依赖“某个历史文件存在”。

```json
{
  "schema_version": "1.0",
  "artifact_id": "art-...",
  "type": "operator_revision|runtime_oplist|accuracy_result|accuracy_comparison|performance_result|release_decision|service_attempt|analysis_request|analysis_result|agent_session|verification_experiment|shadow_comparison",
  "run_id": "run-...",
  "candidate": "v3",
  "step_id": "04_v3_discovery_startup",
  "path": "results/...",
  "sha256": "...",
  "created_at": "...",
  "producer": {
    "tool": "...",
    "version": "..."
  },
  "identity": {
    "model": "...",
    "image_digest": "...",
    "workflow_profile": "plugin_only",
    "operator_revision": "...",
    "service_start_id": "...",
    "dataset": "...",
    "evaluation_digest": "...",
    "config_digest": "..."
  },
  "valid": true,
  "validation_errors": [],
  "_meta": {}
}
```

最低要求：

- Artifact ID 在 run 内唯一；
- 文件内容带 SHA-256；
- 与 run、candidate、step 绑定；
- 关键 Artifact 与模型、镜像 digest、revision 和配置绑定；
- Gate 使用前重新校验存在性和摘要；
- 解析失败、身份不匹配或摘要变化时 fail-closed；
- 同名历史文件不能自动成为本轮证据。

### 4.3 Gate Result

```json
{
  "schema_version": "1.0",
  "gate_id": "v3_accuracy",
  "run_id": "run-...",
  "candidate": "v3",
  "status": "passed|failed|unassessed|error",
  "qualified": false,
  "reason": "reference_missing_or_invalid",
  "inputs": ["art-..."],
  "evaluated_at": "...",
  "details": {},
  "_meta": {}
}
```

Gate 规则：

- 不信任 Claude 文本结论；
- 不信任任意工具直接写入的“成功”布尔值；
- 只读取通过校验的结构化 Artifact；
- 缺失、损坏、口径不兼容时为 `unassessed` 或 `error`，不得放行；
- V3 性能不创建 qualification Gate；
- V4 性能提升只作为 V4 establishment Gate 输入。

### 4.4 Operator Revision

```yaml
schema_version: "1.0"
id: v3-startup-r1
parent_id: v3-discovered
candidate: v3
source: startup_tuning
run_id: ""

sets:
  discovered_ops: []
  requested_enabled_ops: []
  runtime_enabled_ops: []
  disabled_ops:
    startup: []
    accuracy: []
    v4_performance: []

environment:
  VLLM_PLUGINS: fl
  USE_FLAGGEMS: "1"
  VLLM_FL_FLAGOS_WHITELIST: ""

verification:
  service_start_id: ""
  service_ready: false
  runtime_oplist_artifact: ""
  runtime_oplist_fresh: false
  runtime_oplist_matches: false
  mismatch: {}

evidence_artifacts: []
created_at: ""
artifact_id: ""
_meta: {}
```

约束：

- `v3-discovered` 只能来自首次全组件运行时发现；
- startup/accuracy/V4 revision 只能从父 revision 减少算子；
- `v3-final` 必须通过最终精度验证和运行时 oplist 核验；
- `v4-r0` 是 `v3-final` 的不可变克隆；
- V4 不能覆盖任何 V3 Artifact；
- startup、accuracy、v4_performance 禁用原因分别保存。

### 4.5 长任务 Job

```yaml
jobs:
  job-xxx:
    type: accuracy_eval
    candidate: v3
    step_id: 07_v3_accuracy_initial
    idempotency_key: "..."
    command_file: config/jobs/job-xxx.cmd
    state_file: logs/jobs/job-xxx.state.json
    log_file: logs/jobs/job-xxx.log
    status: running
    started_at: ""
    timeout_seconds: 21600
    output_artifacts: []
```

评测、benchmark、搜索和发布继续通过 `task_runner.py` detached 执行。恢复时先检查 state 和 idempotency key，禁止重复启动仍在运行或已有有效输出的任务。

### 4.6 Analysis Request、Result 与 Agent Session

Agent 输入、输出和会话必须结构化并登记为 Artifact。请求至少绑定：

```yaml
schema_version: "1.0"
analysis_type: startup_failure
workflow_run_id: run-001
candidate: v3
operator_revision: v3-startup-r2
input_artifacts: []
allowed_experiments:
  - disable_ops_and_restart
limits:
  max_candidate_ops: 3
  max_tool_rounds: 12
  timeout_seconds: 900
_meta: {}
```

结果至少表达状态、证据和建议实验：

```json
{
  "schema_version": "1.0",
  "analysis_type": "startup_failure",
  "status": "hypothesis_available|no_hypothesis|unresolved|error",
  "suspected_ops": [
    {
      "name": "apply_rotary_pos_emb",
      "confidence": 0.82,
      "evidence_artifacts": ["art-log-001"],
      "evidence_locations": ["service.log:1832-1874"]
    }
  ],
  "recommended_experiment": {
    "type": "disable_ops_and_restart",
    "ops": ["apply_rotary_pos_emb"]
  },
  "_meta": {}
}
```

本地校验必须确认：

- request 的 run、candidate、revision 与当前状态一致；
- 所有输入和引用证据均为有效 Artifact；
- 通常情况下，候选算子必须属于当前 discovered/runtime 集合；
- 仅当本轮新 oplist 尚未生成时，允许候选同时满足“来自本轮 traceback/kernel/诊断日志直接证据”和“属于当前已安装 FlagGems 版本已知算子目录”；该目录只验证算子名与实验是否合法，不能替代 `v3-discovered`；
- 服务恢复后必须使用新生成且通过 freshness/identity 校验的 runtime oplist 建立 `v3-discovered`，受限例外产生的候选不能直接成为官方发现事实；
- 建议实验属于当前任务的策略白名单；
- 建议只创建实验性子 revision，不直接修改已提交 revision；
- 输出超过限制、Schema 无效、证据过期或 identity 不匹配时拒绝执行。

Agent Session 保存 task type、runtime、provider、checkpoint、输入/输出 Artifact 和状态，不把完整对话嵌入 Context。状态至少包括：

```text
pending → running → hypothesis_available | no_hypothesis | unresolved | error
```

Agent 不可用且确定性路径无法继续时，必须生成 `unresolved`、issue 和 manual-handoff Artifact；不得伪造结论或绕过 Gate。

### 4.7 Model Capability Profile（未来 LangGraph 阶段）

未来 provider-neutral 模型配置必须显式声明能力：

```yaml
models:
  diagnostic:
    provider: internal_gateway
    model: model-x
    capabilities:
      tool_calling: true
      parallel_tool_calling: false
      structured_output: json_schema
      streaming: true
      max_context_tokens: 131072
    limits:
      max_output_tokens: 8192
      request_timeout_seconds: 900
      max_tool_rounds: 12
```

router 必须先做能力准入再执行分析。该契约在本轮定义，但 LangGraph 和第三方 Provider 实现属于后续独立阶段，不阻塞当前 Claude Code 适配器交付。

---

## 5. 模块分类

### 5.1 保留并适配

| 模块 | 主要变化 |
|---|---|
| `prompts/run_batch.sh` | 调用 Plugin-only profile，读取新结果语义 |
| `prompts/stream_filter.py` | 支持新 step 和 V3/V4 事件 |
| `prompts/stream_to_debug_log.py` | 记录 Artifact/Gate 摘要 |
| `wait_for_service.sh` | 返回结构化 ready/crash/timeout |
| `service_monitor.py` | 关联 service-start ID 和 attempt Artifact |
| `safe_restart_service.sh` | revision 启动、缓存清理、证据保留 |
| `diagnose_ops.py` | 输出置信度、证据和建议 revision |
| `flagos_op_config.py` | 统一环境、白名单、revision 和核验逻辑 |
| `task_runner.py` | job ID、幂等键和 Artifact 绑定 |
| `eval_monitor.py` | candidate、revision 和 dataset identity |
| `benchmark_runner.py` | `v3_measurement`、`v4_probe`、`v4_final` |
| `error_writer.py` | 新 step/job/artifact/gate/revision 字段 |
| `rollback_overflow.py` | 新 graph 越位回滚 |
| `diagnose_failure.py` | 基于新状态和 Agent Session 生成恢复建议 |
| `workflow/agents/base.py` | 定义 provider-neutral `AnalysisAgent` 接口 |
| `workflow/agents/schemas.py` | 定义 startup/accuracy/unknown 请求与结构化结果 |
| `workflow/agents/router.py` | 按任务、能力和可用性选择分析运行时 |
| `workflow/agents/policy.py` | 校验 Agent 建议、证据引用和受限实验 |
| `workflow/agents/sessions.py` | Agent Session、checkpoint、恢复和审计引用 |
| `workflow/agents/claude_code.py` | 本轮 `ClaudeCodeAnalysisAgent` 适配器 |

### 5.2 重构或替换

| 模块 | 主要变化 |
|---|---|
| `prompts/run_pipeline.sh` | 收敛为薄入口 |
| `shared/context.template.yaml` | 替换为 Schema v2 |
| `shared/update_context.py` | 领域 transition、Artifact ingestion、Gate reducer |
| `shared/generate_report.py` | 按 V3/V4 证据组织报告 |
| `inspect_env.py` | 只输出 Plugin-only admission contract |
| `start_service.sh` | 只向新 graph 暴露 discovery/revision 模式 |
| `apply_op_config.py` | 发现模式和 immutable whitelist revision |
| `persist_op_config.py` | 创建 revision、启动、核验并登记 Artifact |
| `operator_optimizer.py` | 限定为 V3 精度调优能力 |
| `operator_reduction.py` | V4 只以冻结 V3 为基线 |
| `accuracy_compare.py` | `reference/candidate` 语义和 fail-closed |
| `performance_compare.py` | 只做 V4 对 V3 通用比较 |
| release 工具链 | 消费显式 release decision |
| batch/notification 工具 | 读取 establishment、fallback 和 release 状态 |

### 5.3 切换完成后删除

- `skills/flagos-service-startup/tools/baseline_selector.py`
- `prompts/v1_gate.py`
- `prompts/step7_gate.py`
- `prompts/auto_v1v2_pipeline.md`
- `skills/flagos-performance-testing/tools/synthesize_perf_baseline.py`
- Branch A/native/V1/V2/V2.1/V2.2/V3.1/V3.2 路由
- 独立 Plugin 步骤 9–13
- `plugin_workflow.*`
- V2/V3 双 tag 和 `--also-tag` 补偿路径
- V3 Native-relative performance Gate 和 operator search 入口
- 通过旧结果文件名推导恢复和报告状态的代码

物理删除必须放在最后，禁止在新状态、恢复、发布和报告尚未可用时提前删除。

---

## 6. 分段实施计划

## 工作段 0：冻结语义和迁移护栏

### 目标

将目标业务语义写成不可回退的工程不变量，避免实现过程中重新引入 V1、Native 或 V3 性能 Gate。

### 文件范围

- `docs/plugin_only_workflow_optimization.md`
- 本文档
- 新增契约 fixtures/测试常量
- 暂不修改 `CLAUDE.md` 主流程

### 实施

1. 固化以下不变量：
   - profile 固定为 `plugin_only`；
   - discovery 固定 `VLLM_PLUGINS=fl`、`USE_FLAGGEMS=1`；
   - discovery 禁止当前模型 whitelist；
   - external NV accuracy 是唯一业务红线；
   - V3 performance 只产生 measurement Artifact；
   - V4 基线固定为 `v3-final` 和 V3 performance；
   - V4 不成立时回退 V3，不产生伪 V4；
   - Workflow Engine 拥有确定性执行、验证、Gate 和发布决定；
   - Claude Code 仅通过 `AnalysisAgent` 接口参与关键分析；
   - Agent 输出必须经过 suggest–verify–commit，不能直接改变业务状态；
   - LangGraph 未来只替换分析 harness，不替换确定性 graph。
2. 建立旧字段到新字段的迁移对照表。
3. 迁移期允许旧代码存在，但新 profile 不得调用旧分支。
4. 使用临时迁移开关选择新引擎，例如 `FLAGOS_WORKFLOW_PROFILE=plugin_only`；切换完成后移除开关。
5. 固化本轮与未来阶段边界：本轮实现 `AnalysisAgent` 契约和 `ClaudeCodeAnalysisAgent`，后续独立实现 LangGraph/ModelProvider/shadow 切换。

### 验收

- 契约中无 `Primary` 目标术语；
- V3 establishment 不含性能；
- 新 graph 无 Native/V1 入口；
- V4 输入明确绑定不可变 V3。

### 回滚

仅文档和 fixtures，可独立回滚。

---

## 工作段 1：Schema v2、Artifact Registry、Gate Reducer 和状态转换

### 目标

建立后续全部步骤共享的状态和证据基础设施。

### 文件范围

- `shared/context.template.yaml`
- `shared/update_context.py`
- 建议新增：
  - `shared/workflow_state.py`
  - `shared/artifact_registry.py`
  - `shared/gate_reducer.py`
  - `shared/schema/context_v2.schema.json`
  - `shared/schema/artifact_v1.schema.json`
  - `shared/schema/gate_v1.schema.json`
  - `shared/schema/operator_revision_v1.schema.json`
  - `shared/schema/analysis_request_v1.schema.json`
  - `shared/schema/analysis_result_v1.schema.json`
  - `shared/schema/agent_session_v1.schema.json`
  - `shared/schema/verification_experiment_v1.schema.json`
- `setup_workspace.sh`
- 单元测试

### 实施

1. 将 context 模板迁移到 Schema v2。
2. 为 Context、Artifact、Gate、operator revision 建立严格 schema。
3. 拆分 `update_context.py`：
   - 保留通用 ledger/timing 更新；
   - 业务结论只能通过受控 transition；
   - 禁止任意设置 `established=true` 或 Gate `qualified=true`；
   - 删除解析失败后放行的旧逻辑。
4. 实现 Artifact 注册、摘要、身份绑定和校验。
5. 实现 admission、V3 service、V3 accuracy、V3 config、V3 establishment、V3 release、V4 establishment Gate。
6. `setup_workspace.sh` 部署新增工具和 schema。
7. 初始化 context 时生成唯一 run ID。
8. Context 只保存 Agent Session 引用、checkpoint ID 和输入/输出 Artifact，不嵌入完整对话。
9. 如需旧状态导入，只能作为只读诊断，不能把旧布尔值或 Agent 文本直接转换为 Gate 通过。

### 验收

- Artifact 缺失、损坏、身份不匹配均 fail-closed；
- step 成功与 Gate 失败可同时表达；
- V3 performance 结果不影响 V3 establishment；
- 任意命令不能直接设置 V3/V4 established；
- ledger、timing、trace 和 report 可从 Schema v2 获取数据。

### 回滚

保留迁移前 template/updater 快照；旧、新 context 不得混用。

---

## 工作段 2：确定性 Workflow Engine 和薄入口

### 目标

将流程图、状态推进、恢复和收尾从大型 shell 条件分支中抽离。

### 文件范围

- `prompts/run_pipeline.sh`
- `prompts/run_batch.sh`
- 建议新增：
  - `workflow/engine.py`
  - `workflow/graph.py`
  - `workflow/steps.py`
  - `workflow/recovery.py`
  - `workflow/cli.py`
  - `workflow/agents/base.py`
  - `workflow/agents/schemas.py`
  - `workflow/agents/router.py`
  - `workflow/agents/policy.py`
  - `workflow/agents/sessions.py`
  - `workflow/agents/claude_code.py`
- `prompts/stream_filter.py`
- `prompts/stream_to_debug_log.py`

### 实施

1. 用显式有向图描述步骤和前置条件。
2. 每个 step 定义输入 Artifact、执行器、执行结果、Gate、trace 和恢复语义。
3. Workflow Engine 通过 router 决定何时需要 `startup_failure`、`accuracy_regression` 或 `unknown_failure` 分析，不在 shell 中直接拼 Claude Code 调用。
4. 统一生命周期：
   ```text
   validate prerequisites
     → ledger in_progress
     → create/recover job or agent session
     → deterministic execution / bounded analysis
     → register artifacts
     → validate proposed experiment
     → execute deterministic verification
     → reduce gates
     → write trace/timing
     → regenerate report
   ```
5. 恢复逻辑以 Context、job state、Agent Session、Artifact 摘要和 revision ID 为依据。
6. `run_pipeline.sh` 仅启动 `workflow/cli.py`。
7. `run_batch.sh` 不再解析旧文件名或 Agent 文本判断模型成功。
8. 更新流式日志步骤名和 Analysis Agent 事件。

### 验收

- 同一 Context 和 Artifact 集合得到相同下一步；
- running job 不重复启动；
- 有效已完成步骤不重跑；
- 同名但摘要不匹配的文件不被复用；
- shell 不再维护 V1/V2/Plugin 状态机；
- 每步结束同步更新 ledger、trace、timing、report。

### 回滚

迁移期开关可回到旧入口；新引擎端到端通过前不删除旧入口逻辑。

---

## 工作段 3：Plugin-only 准入

### 目标

把环境分类改为严格的完整组件准入，不再路由 A/B/native pipeline。

### 文件范围

- `inspect_env.py`
- `skills/flagos-pre-service-inspection/SKILL.md`
- `setup_workspace.sh`
- admission fixtures/tests

### 实施

1. 保留组件、版本、能力和 GPU 探测。
2. 新 profile 不再依赖 `gems_tree/gems_tree_plugin/native` 和 `A/B/native` 输出。
3. 输出结构化 admission Artifact：
   ```json
   {
     "profile": "plugin_only",
     "accepted": true,
     "components": {
       "vllm": {},
       "flaggems": {},
       "flagtree": {},
       "vllm_plugin_fl": {}
     },
     "capabilities": {},
     "rejection_reasons": []
   }
   ```
4. 必须确认 vLLM、FlagGems、FlagTree、vllm-plugin-FL 均存在，Plugin 可由 `VLLM_PLUGINS=fl` 加载并支持所需白名单能力。
5. 不执行组件安装或 Native fallback。
6. 不支持的镜像结构化拒绝，不进入服务阶段。

### 验收

- 完整组件环境通过；
- 缺任一组件或 Plugin 无法加载时拒绝；
- `gems_tree` 不再路由到 Branch A；
- 不调用安装脚本；
- 不写 `entry_image_type/pipeline_branch`。

### 回滚

旧 profile 可暂时保留旧分类器；Plugin-only profile 只使用新 contract。

---

## 工作段 4：V3 首次全组件发现启动和 oplist Artifact

### 目标

保证当前模型初始算子集合来自本轮全组件运行时，而非历史文件或预置白名单。

### 文件范围

- `start_service.sh`
- `wait_for_service.sh`
- `service_monitor.py`
- `safe_restart_service.sh`
- `skills/flagos-service-startup/SKILL.md`
- `flagos_op_config.py`
- 建议新增 `capture_runtime_oplist.py`、`service_attempt.py`

### 实施

1. 新 graph 只调用两种显式模式：`discovery`、`revision`。
2. discovery 固定：
   ```bash
   VLLM_PLUGINS=fl
   USE_FLAGGEMS=1
   ```
   并清除当前模型 whitelist/blacklist 控制输入。
3. 启动前：停止遗留服务、释放 GPU、清编译缓存、归档旧 oplist、创建 service-start ID、记录开始时间和独立日志。
4. 启动后验证 oplist：本轮生成、mtime 晚于启动时间、内容可解析，并绑定模型、镜像和 service-start ID。
5. 原始 oplist 与解析结果均登记为 Artifact。
6. 即使服务崩溃，也保存日志、部分 oplist 和完整性结论；部分或完整性不确定的 oplist 不得建立 `v3-discovered`。
7. 如果本轮新 oplist 尚未生成，只允许使用本轮 traceback/kernel/诊断日志中的直接证据，并通过当前已安装 FlagGems 版本的已知算子目录校验实验候选合法性；该目录不构成官方发现集合，服务恢复后仍须捕获并校验新的 runtime oplist。
8. 产出：
   ```text
   results/operator-configs/v3-discovered-oplist.txt
   results/operator-configs/v3-discovered.json
   results/service-attempts/<service_start_id>.json
   ```
9. discovery 成功后创建 `v3-discovered`，但不直接等同 `v3-startup-stable`。

### 验收

- discovery 命令必含两个固定变量；
- discovery 无当前模型 whitelist；
- 旧 oplist 不会被误用；
- mtime 早于启动时间的 oplist 无效；
- oplist 解析失败时不能建立 verified revision；
- 服务崩溃时仍保留证据；
- 每次启动前清理三类编译缓存；
- 新 graph 无法触达 `USE_FLAGGEMS=0`。

### 回滚

保留旧启动模式为不可达兼容代码，待后续集成测试完成再删除。

---

## 工作段 5：V3 启动兼容性算子调优

### 目标

将首次启动崩溃处理实现为确定性 revision 迭代，禁止通过 Native fallback、关闭组件或临时参数规避。

### 文件范围

- `diagnose_ops.py`
- `apply_op_config.py`
- `persist_op_config.py`
- `flagos_op_config.py`
- `ops_constants.py`
- `skills/flagos-operator-replacement/SKILL.md`
- workflow startup-tuning step

### 实施

1. `diagnose_ops.py` 输出：
   ```json
   {
     "crashed_ops": [],
     "candidate_ops": [],
     "evidence": [],
     "confidence": {},
     "attributable_operator": "",
     "diagnosis_exhausted": false
   }
   ```
2. 固定诊断顺序：本轮日志 → 本轮 oplist → `crashed_ops` → `candidate_ops` → `flag_gems` traceback → 最后 kernel。
3. 确定性证据不足、矛盾或需要跨日志归因时，由 Workflow Engine 创建 `StartupFailureRequest`，调用 `AnalysisAgent`。
4. Agent 只返回结构化候选、证据引用和受限实验建议；Schema 或 policy 校验失败时拒绝建议并记录失败 Artifact。
5. Policy 默认要求候选属于当前 discovered/runtime 集合；仅在本轮新 oplist 尚未生成时，才允许“本轮直接日志证据 + 当前安装版本已知算子目录”的受限例外。该目录不能生成或补全 `v3-discovered`。
6. 每项合法建议先基于父 revision 创建实验性子 revision，加入候选 `disabled_ops.startup`，再清缓存、以 Plugin whitelist 重启、核验 runtime oplist 并登记 `verification_experiment`。
7. 只有服务实测恢复且原故障消失时才提交 child revision；验证失败则回滚父 revision并保存负证据，防止重复尝试同一无效假设。
8. revision 启动固定完整组件：
   ```bash
   VLLM_PLUGINS=fl
   USE_FLAGGEMS=1
   VLLM_FL_FLAGOS_WHITELIST=<enabled ops>
   ```
9. 有新可归因算子时持续迭代。
10. 服务就绪后必须以新生成且通过 freshness/identity 校验的 runtime oplist 建立或确认 `v3-discovered`；只有 runtime oplist 核验通过时才冻结 `v3-startup-stable`。
11. 诊断穷尽时生成 issue，service Gate 不通过，进入诊断收尾，不切 Native。

### 验收

覆盖首次成功、单算子崩溃、多轮累计禁用、部分/缺失 oplist、高低置信候选、非法 Claude 建议、runtime oplist 不匹配和无法归因场景。每轮必须具备 revision、trace、ledger、timing、日志和恢复点。

### 回滚

可回到任意不可变父 revision；不得覆盖原始 discovery Artifact。

---

## 工作段 6：V3 外部 NV 精度评测和精度算子调优

### 目标

将旧 V1/V2 比较迁移为外部 reference 与 candidate 比较，并使精度调优建立在 `v3-startup-stable` 上。

### 文件范围

- `shared/nv_baseline.yaml`
- `accuracy_compare.py`
- `eval_monitor.py`
- `eval_wrapper.py`
- `persist_tuning_checkpoint.py`
- `task_runner.py`
- `skills/flagos-eval-comprehensive/SKILL.md`
- `operator_optimizer.py`
- V3 accuracy steps

### 实施

1. `accuracy_compare.py` 使用通用 CLI：
   ```text
   --reference <manifest-or-reference-id>
   --candidate <result-artifact>
   --dataset <name>
   --threshold 0.05
   ```
2. 输出 `reference_score`、`candidate_score`、`relative_drop`、`qualification_status`、`qualified`、`reason`。
3. external NV reference 校验模型、dataset、metric、评测配置/版本、采样口径、score 和来源。
4. reference 缺失、解析失败或口径不兼容时 fail-closed。
5. 每个数据集生成独立 eval/comparison Artifact，整体 Gate 做 AND 归约。
6. 初次全部达标时从 `v3-startup-stable` 形成待终检 `v3-final`。
7. 不达标时先执行确定性精度调优：只减少算子，记录 `disabled_ops.accuracy`，每轮重启并核验 oplist，按上限重新评测。
8. 当数据集证据冲突、候选难以排序或确定性搜索未在预算内收敛时，创建 `AccuracyRegressionRequest` 并调用 `AnalysisAgent`。
9. Agent 只可返回候选排序、证据引用和受限实验；Engine 为合法建议创建实验性 accuracy revision，执行完整服务重启、oplist 核验和对应数据集评测。
10. 只有实测精度 Artifact 验证建议有效时才提交 revision；Agent 不得直接写 `disabled_ops.accuracy` 或精度 Gate。
11. 发布前对最终 revision 做全部数据集终检。
12. 只有全部数据集通过且 runtime 配置核验通过，才能建立 `v3-final`。

### 验收

覆盖单/多数据集、5% 边界、reference 缺失/损坏/不匹配、step 成功但 Gate 失败、revision 只减不增、禁用原因分离、终检 digest 一致和长任务恢复。

### 回滚

可回到 `v3-startup-stable` 或任一 accuracy revision；历史评测 Artifact 不覆盖。

---

## 工作段 7：V3 性能改为纯测量

### 目标

移除 V3 的 Native/V1 比较、合成基线、ratio Gate 和性能搜索，只保留绝对性能结果。

### 文件范围

- `benchmark_runner.py`
- 两份 `perf_config.yaml`
- `skills/flagos-performance-testing/SKILL.md`
- `performance_compare.py`
- `operator_search.py`
- V3 performance step

### 实施

1. benchmark 模式明确为 `v3_measurement`、`v4_probe`、`v4_final`。
2. V3 只通过 `benchmark_runner.py` 执行，输出 `results/performance/v3-performance.json`。
3. Artifact 绑定 `v3-final`、service-start ID、benchmark config digest、模型和镜像。
4. 不生成 Native/V1 ratio、`performance_ok`、qualification、synthetic baseline 或 V3 operator search。
5. 工具执行失败按 step execution failure 处理；数值高低只进报告。
6. `performance_compare.py` 从 V3 路径断开，只供 V4 对 V3 比较。
7. 合并重复 perf config，确定唯一权威来源。

### 验收

- V3 只调用 `benchmark_runner.py`；
- 不读取 `native_performance.json`；
- 不调用 synthetic baseline 或 V3 performance search；
- 任意低性能不影响 V3 establishment；
- 工具失败不被伪装成“性能不达标”；
- Artifact 与 `v3-final` 匹配。

### 回滚

旧比较工具先保留但从新 graph 断开。

---

## 工作段 8：V3 冻结、成立判定和发布策略

### 目标

通过显式 Gate 和 release decision 发布 V3，区分正式交付与私有诊断产物。

### 文件范围

- `skills/flagos-release/tools/main.py`
- `src/config.py`
- `src/stages/publish.py`
- `upload_to_platform.py`
- `verify_release_consistency.py`
- `skills/flagos-release/SKILL.md`
- V3 freeze/release step

### 实施

1. V3 establishment：
   ```text
   service_ready
   AND final_accuracy_qualified
   AND runtime_operator_config_verified
   ```
2. V3 performance 不参与 Gate。
3. 发布前生成不可变 V3 bundle，包含 revision、runtime oplist、全部精度结果、V3 performance、模型/镜像/组件版本和 Gate。
4. release 工具只消费显式 decision：
   ```json
   {
     "candidate": "v3",
     "established": true,
     "release_class": "qualified",
     "tag_suffix": "-v3",
     "destinations": ["harbor", "modelscope", "huggingface"],
     "bundle_artifact": "art-..."
   }
   ```
5. 精度失败或 reference 无效时，只执行既定 Harbor 私有诊断发布，不更新正式外部模型仓库。
6. 发布后校验镜像 tag、revision、bundle 和报告一致。
7. 新路径不使用 `--also-tag`、Plugin alias 或 V2/V3 双 tag。

### 验收

覆盖正式 V3、私有诊断 V3、reference 无效、低性能但精度通过、bundle/revision 不一致、凭证和网络失败。不得产生 V2 tag。

### 回滚

发布前 release decision 是边界；外部推送中断只能幂等恢复同一 bundle，不能以相同 tag 推送不同内容。

---

## 工作段 9：V4 绑定不可变 V3 的性能优化

### 目标

V4 只比较 V3 实测性能，并继续使用同一外部 NV 精度红线。

### 文件范围

- `operator_reduction.py`
- `flagos_op_config.py`
- `benchmark_runner.py`
- `performance_compare.py`
- `accuracy_compare.py`
- V4 workflow steps

### 实施

1. `v4-r0` 是 `v3-final` 的不可变克隆。
2. 移除 V1/native throughput、V2 final set、Native ratio 和 `performance_ok` 输入。
3. 阶段一性能搜索：逐步减少算子；每个候选建立 revision；每轮重启并核验 oplist；通过 `v4_probe` 测量；只在绝对性能优于当前最优时推进；至少保留一个算子。
4. 性能比较固定为 V3 reference 对 V4 candidate，不手工计算。
5. 阶段二按性能从高到低做精度回溯，使用同一 external NV reference。
6. 对选中候选执行最终性能和精度终检。
7. V4 establishment：
   ```text
   search_execution_success
   AND performance_improved_over_v3
   AND accuracy_qualified_against_external_nv
   AND retained_operator_count >= 1
   AND runtime_operator_config_verified
   ```
8. 无合法提升时：
   ```yaml
   execution_success: true
   established: false
   fallback_to_v3: true
   reason: no_valid_improvement
   ```
9. V4 未成立不修改 V3，也不发布 V3 等价的 V4。

### 验收

覆盖合法提升、最快候选精度失败后回溯、全部候选精度失败、无性能提升、单算子下限、oplist 不匹配、V4 失败不污染 V3、fallback 时不发布 V4。

### 回滚

V4 全部建立在 V3 不可变快照上，任何失败直接回退 V3。

---

## 工作段 10：恢复、报告、trace、批处理和通知迁移

### 目标

让外围模块以 Context v2、Artifact、Gate 和 revision 为唯一事实来源。

### 文件范围

- `shared/generate_report.py`
- `shared/error_writer.py`
- `shared/rollback_overflow.py`
- `diagnose_failure.py`
- `tools/batch_summarize/*`
- `tools/notifications/*`
- 对应 tests

### 实施

1. 报告改为 admission、discovery、startup tuning、V3 精度、V3 absolute performance、V3 release、V4 search/backtrack/release，并增加 Analysis Agent 专章。
2. Agent 专章必须区分：runtime/provider、会话状态、结构化假设、验证实验、实测结论和 `unresolved`/manual handoff；Agent 文本一律标记为“待验证分析”，不能呈现为事实。
3. 不展示不存在的 V1/V2 性能比或综合 performance qualification。
4. 恢复诊断根据 step、job、Agent Session、checkpoint、Artifact、revision、Gate、service attempt 和 verification experiment 决定恢复点。
5. 恢复中的 Agent Session 按以下规则处理：
   - `pending`：可安全启动；
   - `running` 且 runtime 仍可恢复：从 checkpoint 恢复，不新建重复会话；
   - `running` 但 runtime 已丢失：标记原会话 `error`，新建有显式父引用的重试会话；
   - `hypothesis_available`：若实验未执行，只恢复确定性验证，不重新分析；
   - `unresolved`：保留诊断收尾和人工接管入口，不自动绕过 Gate。
6. 工具失败后继续读取 `_last_error.json`，写入新上下文；Analysis Agent 失败还要注册 `analysis_result`/`agent_session` 错误 Artifact。
7. batch 显式展示：
   ```text
   v3.established
   v3.release.status
   v4.execution_success
   v4.established
   v4.fallback_to_v3
   v4.release.status
   analysis.session_count
   analysis.unresolved_count
   analysis.manual_handoff_required
   ```
8. 通知和 trace 使用新 step ID、Artifact 引用、Gate 结果、Agent Session ID 和 verification experiment ID。通知不得把 Agent 假设写成已确认根因。
9. issue 由确定性故障事实和验证结果生成；可附带 Agent 分析摘要，但必须分别标注“假设”“已验证”“已否证”。
10. 最终生成 `context_final.yaml` 和 Artifact index；索引必须覆盖 Agent request/result/session/experiment。

### 验收

- 报告不按旧文件名或 Agent 自然语言推断状态；
- V3 performance 只展示绝对值；
- V4 fallback 明确显示最终交付仍为 V3；
- discovery、startup tuning、accuracy、benchmark、release、V4 search 和 Agent Session 中断均可恢复；
- `hypothesis_available` 会话恢复后只补做尚未完成的确定性验证；
- Agent 不可用时显示 `unresolved` 和 manual handoff，不伪造根因或 Gate；
- 损坏 Artifact 不被报告为成功；
- 每个 step 都有 ledger、trace、timing 和最新报告。

### 回滚

迁移期可并行生成旧、新报告，但 Gate 和发布只能读新状态。

---

## 工作段 11：项目指令和 Skill 文档切换

### 目标

在可执行行为已完成后更新仓库指令，避免文档先行造成脚本与指令不一致。

### 文件范围

- `CLAUDE.md`
- 相关 `skills/*/SKILL.md`
- `docs/project_guide.md`
- `docs/notification_and_result_analysis_design.md`
- `docs/optimization_metrics_report.md`
- 其他双 pipeline/V1/V2/Plugin 9–13 文档

### 实施

1. 主流程改为 Plugin-only V3/V4。
2. 删除 Branch A/native、V1 三选、V2/V3 补偿和独立 Plugin 阶段说明。
3. 明确 discovery、startup tuning、external NV fail-closed、V3 measurement-only 和 V4 fallback。
4. 明确确定性 Workflow Engine 与 Analysis Agent 的职责边界：Engine 拥有执行、验证、Artifact、Gate、revision、状态转换和发布决定；Agent 只分析并提出受限实验。
5. 将 Claude Code 描述为本轮 `ClaudeCodeAnalysisAgent` 运行时，不再把“Claude 自行修改状态/算子”写成合法流程。
6. 在启动、精度和未知故障 Skill 中统一记录 Analysis Request/Result、Schema/Policy 校验、verification experiment 和 commit/rollback 规则。
7. 明确 Agent 不可用、输出非法或证据不足时进入 `unresolved`/manual handoff，禁止绕过 Gate。
8. 更新长任务、Agent Session 恢复、trace、ledger、report 和问题日志 step 名称。
9. 未来 LangGraph 仅作为独立迁移阶段记录：替换 Analysis Agent harness，通过 ModelProvider 接入模型 API，不替换 15 步确定性 Engine。
10. 历史文档保留时明确标注“历史方案”。

### 验收

- 文档命令和实际 CLI 一致；
- step ID 与 graph 一致；
- 不再要求 baseline selector、synthetic baseline 或 V3 performance tuning；
- 无 `Primary` 目标术语；
- 所有文档均使用同一 `AnalysisAgent`、suggest–verify–commit 和 `unresolved` 语义；
- 无文档授权 Agent 直接修改 committed revision、Gate 或 release decision；
- LangGraph 被明确列为后续独立阶段，不成为本轮交付前置条件；
- V0 明确为未来扩展。

### 回滚

文档单独提交，可独立回滚。

---

## 工作段 12：旧代码断链和物理删除

### 目标

新链路端到端验证后，清除旧双 pipeline 和基线语义。

### 前置条件

- 至少完成一次成功 V3；
- 至少验证一次 V4 成功或正常 fallback；
- 恢复、发布、报告、batch 和 notification 已迁移；
- 新项目指令已上线。

### 实施

1. 静态搜索确认无新路径引用。
2. 删除旧文件、CLI 参数、setup 部署项、schema 字段和旧 fixtures。
3. 删除范围包括：baseline selector、V1/step7 gates、auto V1/V2 prompt、synthetic baseline、旧分支路由、独立 Plugin 步骤、双 tag 和旧文件名推断。
4. `toggle_flaggems.py` 如为未来 V0 保留，必须与 Plugin-only graph 隔离。
5. `operator_search.py` 如仍有可复用能力，先拆出通用 primitives，再删除 Native-relative 编排。
6. `performance_compare.py` 若保留，只保留通用 reference/candidate CLI。

### 验收

全仓新运行路径不得引用：

```text
baseline_selector
workflow.performance_ok
plugin_workflow
synthesize_perf_baseline
pipeline_branch: A
V1.1/V1.2/V1.3
native_performance.json
```

历史文档中的明确历史引用可豁免。

### 回滚

旧代码删除独立提交，出现遗漏时整体回滚该提交，不回滚新状态和 Artifact 架构。

---

## 工作段 13：端到端迁移验证和正式切换

### 验证矩阵

| 场景 | 预期结果 |
|---|---|
| 首次启动成功、精度通过 | 建立并发布 V3，测量性能，进入 V4 |
| 首次启动单算子崩溃 | 创建 startup revision，禁用后恢复 |
| 确定性启动证据不足、Agent 命中问题算子 | 建议通过 policy 校验和实测验证后才提交 revision |
| 首次崩溃且尚无新 oplist、候选有本轮直接证据 | 仅以安装版本已知算子目录校验实验合法性；恢复后用新 oplist 建立发现集合 |
| 尚无新 oplist、候选仅来自旧文件或通用目录 | 拒绝实验，不构造 `v3-discovered`，保留 unresolved/manual handoff |
| Agent 建议非法算子或越权实验 | 拒绝建议，记录 policy failure，不修改 revision |
| Agent 建议未修复故障 | 回滚实验 child revision，保存负证据，避免重复实验 |
| Agent 输出 schema 非法 | 会话标记 `error`，不执行建议，不改变 Gate |
| Agent 引用旧 run/revision/Artifact | identity 校验失败，拒绝分析结果 |
| Agent 不可用且复杂分析必需 | 会话 `unresolved`，生成 manual handoff，不绕过 Gate |
| Agent Session 中断 | 从 checkpoint 或结构化会话状态恢复，不重复已完成实验 |
| 首次启动多轮崩溃 | 累计禁用，lineage 完整 |
| 崩溃且无可归因算子 | service Gate 不通过，issue + 诊断收尾，不切 Native |
| external NV 缺失 | accuracy `unassessed`，V3 不正式成立 |
| 一个数据集失败 | 触发调优；最终失败则仅诊断发布 |
| 精度证据冲突且 Agent 提议候选 | 创建实验 revision，完整重启、oplist 核验和评测后决定提交 |
| V3 性能很低 | 只报告，不阻塞 V3 |
| benchmark 工具失败 | 记录执行异常，不伪造 measurement |
| V4 找到合法提升 | 建立并条件发布 V4 |
| V4 最快候选精度失败 | 回溯次优候选 |
| V4 无合法提升 | fallback V3，不发布 V4 |
| 任意长任务中断 | 从 job state 恢复，不重复运行 |
| Artifact 被篡改 | Gate fail-closed |
| 发布中断 | 同一 bundle 幂等恢复 |
| LangGraph shadow 与 Claude Code 不一致（未来） | shadow 不控制流程，记录 comparison Artifact 和实测差异 |

### 正式切换条件

1. Schema、Artifact、Gate 和 revision 单元测试通过；
2. discovery/startup tuning 集成测试通过；
3. external NV 多数据集 Gate 测试通过；
4. V3 measurement-only 测试通过；
5. V3 正式/诊断发布决策测试通过；
6. V4 success/fallback 测试通过；
7. 至少完成一次中断恢复演练；
8. report、batch 和 notification 已读新 schema；
9. 旧代码引用静态检查通过；
10. `CLAUDE.md`/SKILL 与代码一致。

---

## 7. 新步骤编号

| Step ID | 名称 | 核心输出 |
|---|---|---|
| `01_container_preparation` | 容器和工作目录准备 | workspace/context |
| `02_plugin_only_admission` | 完整组件准入 | admission Artifact/Gate |
| `03_runtime_cleanup` | 旧状态、oplist 和缓存清理 | cleanup trace |
| `04_v3_discovery_startup` | V3 全组件发现启动 | service attempt/discovered oplist |
| `05_v3_startup_tuning` | V3 启动兼容性调优 | startup revision 链 |
| `06_v3_startup_freeze` | 冻结启动稳定集合 | `v3-startup-stable` |
| `07_v3_accuracy_initial` | V3 初始精度 | per-dataset comparisons |
| `08_v3_accuracy_tuning` | V3 精度调优 | accuracy revision 链 |
| `09_v3_accuracy_final` | V3 最终精度 | Gate/`v3-final` |
| `10_v3_performance_measurement` | V3 性能测量 | V3 performance Artifact |
| `11_v3_freeze_release` | V3 冻结和发布 | bundle/release decision |
| `12_v4_performance_search` | V4 性能搜索 | candidate revisions |
| `13_v4_accuracy_backtrack` | V4 精度回溯和终检 | `v4-final` 或 fallback |
| `14_v4_conditional_release` | V4 条件发布 | release result |
| `15_finalize` | 报告、回传和资源清理 | report/context_final/index |

步骤 08 在初始精度达标时可 `skipped`，但必须引用 Gate 并写明 skip reason。步骤 14 在 V4 未成立时可 `skipped`，原因必须来自 V4 establishment Gate。

---

## 8. 产物布局

```text
results/
├── artifacts/index.json
├── admission/admission.json
├── service-attempts/<service_start_id>.json
├── analysis/
│   ├── requests/<agent_session_id>.json
│   ├── results/<agent_session_id>.json
│   ├── sessions/<agent_session_id>.json
│   ├── verification-experiments/<experiment_id>.json
│   └── shadow-comparisons/<comparison_id>.json  # 未来 LangGraph 阶段
├── operator-configs/
│   ├── v3-discovered-oplist.txt
│   ├── v3-discovered.json
│   ├── v3-startup-r1.json
│   ├── v3-startup-stable.json
│   ├── v3-accuracy-r1.json
│   ├── v3-final.json
│   ├── v4-r0.json
│   ├── v4-r1.json
│   └── v4-final.json
├── accuracy/
│   ├── v3-initial-<dataset>.json
│   ├── v3-final-<dataset>.json
│   ├── v3-compare-<dataset>.json
│   ├── v4-candidate-<revision>-<dataset>.json
│   └── v4-final-<dataset>.json
├── performance/
│   ├── v3-performance.json
│   ├── v4-<revision>-performance.json
│   └── v4-final-performance.json
├── gates/
│   ├── admission.json
│   ├── v3-accuracy.json
│   ├── v3-establishment.json
│   └── v4-establishment.json
├── release/
│   ├── v3-decision.json
│   ├── v3-result.json
│   ├── v4-decision.json
│   └── v4-result.json
└── report.md
```

物理路径不是恢复依据；Artifact ID、identity 和摘要才是权威索引。

---

## 9. Trace、日志和报告

### Trace

每个 step 的 trace 新增：

```json
{
  "step": "05_v3_startup_tuning",
  "status": "success",
  "input_artifacts": [],
  "output_artifacts": [],
  "operator_revision_before": "v3-discovered",
  "operator_revision_after": "v3-startup-stable",
  "gate_updates": [],
  "job_ids": [],
  "agent_session_ids": ["agent-session-001"],
  "analysis_artifacts": ["art-analysis-request-001", "art-analysis-result-001"],
  "verification_experiment_ids": ["verify-exp-001"],
  "_meta": {}
}
```

Trace 中的 Agent 字段只表示分析和实验曾发生；revision 变更必须同时引用通过的 `verification_experiment`，不能仅引用 `analysis_result`。

### 问题日志

- `issues_startup.log`：discovery/startup tuning；
- `issues_accuracy.log`：V3/V4 精度异常；
- `issues_performance.log`：V4 搜索异常或 benchmark 工具错误；
- `issues_analysis.log`：Agent schema/policy 错误、runtime 不可用、`unresolved` 和 manual handoff。

V3 性能数值低本身不是 issue。Agent 提议必须按“假设 / 已验证 / 已否证 / 未解决”标注，禁止将未验证假设写成问题根因。

### 报告

报告必须同时展示：

- step execution status；
- Gate qualification status；
- Artifact 校验状态；
- operator revision；
- V3/V4 establishment；
- release class/destination；
- V4 fallback reason；
- Analysis Agent runtime/provider 和会话状态；
- 每项结构化假设及其证据引用；
- verification experiment 的实测结果；
- `unresolved` 数量、原因和 manual handoff 位置；
- 未来 shadow 模式下的对照结果，且明确 shadow 不控制工作流。

禁止把“步骤成功”显示成“版本达标”，也禁止把“Agent 提出假设”显示成“根因已确认”。

---

## 10. 测试体系

### 单元测试

覆盖：

- Context schema、Artifact identity/hash、Gate fail-closed；
- revision 集合约束和 oplist freshness；
- accuracy relative drop、V3/V4 establishment 和 release decision；
- Analysis Request/Result/Agent Session/verification experiment schema；
- Agent 结果中的 run、candidate、revision 和 Artifact identity 校验；
- operator membership、允许实验、候选数、输出大小、timeout 和 tool-round policy；同时覆盖正常 discovered/runtime 成员约束，以及“尚无新 oplist 时必须具备本轮直接证据并通过安装版本目录校验”的受限例外；
- 已知算子目录只提供合法性校验，不能创建、补全或替代 `v3-discovered`；
- 只有通过实测 verification experiment 才能提交 revision。

### 契约测试

固定以下工具和接口输入输出 schema：

- `inspect_env.py` admission；
- `start_service.sh` attempt result；
- `diagnose_ops.py` diagnosis；
- `accuracy_compare.py` comparison；
- `benchmark_runner.py` measurement；
- `operator_reduction.py` V4 summary；
- release decision/result；
- `AnalysisAgent` startup/accuracy/unknown-failure request/result；
- `ClaudeCodeAnalysisAgent` adapter 的结构化输出、错误映射和 session 状态；
- policy validator 和 verification experiment reducer，包括无新 oplist 例外的直接证据、安装版本目录和恢复后 freshness/identity 契约。

### 集成测试

模拟 oplist 生成、服务 crash/ready/timeout、算子诊断、多数据集精度、benchmark、V4 回溯和 release dry-run，并覆盖：

- 确定性诊断充分时不调用 Agent；
- 证据不足时 router 创建正确的 Analysis Request；
- 合法 Agent 建议经过 suggest–verify–commit 后提交 revision；
- 首次崩溃且无新 oplist 时，只有绑定本轮直接证据并通过安装版本目录校验的候选可进入实验；旧 oplist、无证据目录枚举和凭空候选均被拒绝；
- 受限例外恢复服务后，未生成并通过 freshness/identity 校验的新 oplist 时不得建立 `v3-discovered` 或冻结 `v3-startup-stable`；
- 非法、越权或 stale 建议被拒绝；
- 验证失败时回滚 child revision 并登记负证据；
- Agent 不可用时显式 `unresolved`，其他可执行确定性收尾继续；
- 未知故障路由不会直接改变任何 Gate。

### 恢复测试

在 discovery、startup tuning、accuracy、benchmark、V4 search、release 和 Agent Session 中断，验证：

- 不重复运行有效 job；
- 不复用损坏 Artifact；
- 不覆盖 revision；
- 不跳过未完成 Gate；
- `hypothesis_available` 后只恢复尚未完成的验证实验；
- session checkpoint 可恢复，丢失 runtime 时创建带 parent 引用的重试会话；
- `unresolved` 在恢复后仍不自动放行；
- report/ledger 与实际一致。

### Agent 安全与确定性测试

- Agent 工具只能访问当前 run 白名单路径和已注册 Artifact；
- secrets 在请求、日志和工具结果中被脱敏；
- 只读工具不能修改 revision，写工具只能创建实验性 child；
- 相同有效 Artifact 和相同结构化 Agent Result 产生相同实验与 reducer 结论；
- Agent prose、confidence 和 provider 名称均不能直接影响 Gate；
- 超时、超轮数、超输出和不支持 capability 均产生显式错误或 `unresolved`。

### 未来 LangGraph shadow 测试（不属于本轮上线门槛）

- Claude Code active 与 LangGraph shadow 接收同一份冻结 Artifact 输入；
- shadow 结果只写 `shadow_comparison`，不创建控制流程的实验；
- 比较 Top-1/Top-3 命中、非法建议、Schema 失败、实测恢复、轮数、延迟、token 和成本；
- provider capability 不足时按声明降级或回退，不静默假定工具能力。

### 静态回归检查

新 profile 的 graph、state 和 SKILL 文档中禁止出现：

```text
USE_FLAGGEMS=0
VLLM_PLUGINS=''
baseline_selector
synthesize_perf_baseline
workflow.performance_ok
plugin_workflow
native_performance.json
```

通用工具库或明确标记的历史文档可豁免。

---

## 11. 提交边界

建议按以下可回滚边界提交；实际提交前仍需用户明确授权：

1. `docs: freeze plugin-only v3 v4 contracts`
2. `feat(state): add schema v2 artifacts and gates`
3. `feat(agent): define analysis contracts sessions and policy`
4. `refactor(workflow): add deterministic plugin-only engine`
5. `feat(agent): add claude code analysis adapter`
6. `refactor(admission): require full flagos component stack`
7. `feat(startup): discover runtime operators on full-stack launch`
8. `feat(operator): add verified v3 startup revisions`
9. `refactor(eval): qualify v3 against external nv references`
10. `refactor(perf): make v3 benchmark measurement-only`
11. `refactor(release): publish from explicit v3 decisions`
12. `refactor(v4): optimize from immutable v3 baseline`
13. `refactor(report): migrate recovery agent data and notifications`
14. `docs: switch project instructions to plugin-only workflow`
15. `cleanup: remove legacy v1 v2 and dual-pipeline paths`
16. `test: add workflow agent and recovery coverage`

`AnalysisAgent` 契约与 `ClaudeCodeAnalysisAgent` adapter 分开提交，便于替换或回滚运行时而不影响确定性 Engine。旧代码删除必须单独提交，方便发现遗漏时回滚，而不撤销新状态、Artifact 和 Agent 契约。

---

## 12. 主要风险与控制

| 风险 | 控制措施 |
|---|---|
| 崩溃前 oplist 未完整生成 | 保存部分证据，结合日志/kernel 诊断，不伪造 discovered 集合 |
| 历史 oplist 污染 | 启动前清理、service-start ID、mtime 和摘要核验 |
| 旧布尔值绕过 Gate | 业务成功只能由 Gate reducer 写入 |
| external NV 口径不一致 | reference manifest、兼容性校验、fail-closed |
| V3 性能旧逻辑暗中门控 | graph 无 performance Gate，增加契约和静态测试 |
| V4 修改 V3 | immutable revision 和 bundle digest |
| 中断后重复任务 | idempotency key、state-file 和 Agent Session checkpoint recovery |
| report 与实际不一致 | 只读 Context v2 和有效 Artifact |
| 提前删旧代码 | 先断链、验证、最后独立删除提交 |
| Analysis Agent 误判算子 | 结构化建议、集合约束、策略校验、实验性 child revision 和实测验证 |
| Agent 越权写 Gate/revision | 分离工具权限；Agent 只能产出 request/result，Reducer 和 Engine 独占提交权限 |
| Agent 引用 stale/跨 run 证据 | 对 run、candidate、revision、Artifact identity 和摘要做本地校验 |
| Agent 不可用或会话丢失 | checkpoint 恢复；无法恢复则 `unresolved` + manual handoff，不绕过 Gate |
| 不同模型输出能力不一致 | capability profile、严格 schema、受控降级、provider 回退或 `unresolved` |
| Agent 日志泄露凭证或无关数据 | Artifact 白名单、路径限制、输出上限、secret redaction 和审计日志 |
| LangGraph shadow 意外影响主流程 | shadow adapter 无执行/提交权限，只能写 comparison Artifact |
| 发布精度不达标版本 | release decision 强制引用 Gate 和 bundle |
| 发布伪 V4 | 必须优于 V3、精度通过且 runtime 配置核验 |

---

## 13. 后续独立阶段：LangGraph 与多模型 Provider 迁移

本节不是本轮 Plugin-only V3/V4 重构的上线前置条件，也不应把 LangGraph 代码、V0 语义或 provider 选择逻辑提前塞入当前确定性主流程。

### 目标边界

1. 保持 `AnalysisAgent` 请求、结果、会话和验证实验契约不变；
2. 新增 `LangGraphAnalysisAgent`，只替换 Claude Code 分析 harness；
3. 通过 `ModelProvider` 适配 Anthropic、OpenAI、OpenAI-compatible、内部网关或本地模型服务；
4. 确定性 Workflow Engine、Artifact Registry、Gate Reducer、operator revision 和 release decision 不迁移到 LangGraph；
5. Claude Code 在验证期保留为 active runtime 或 fallback。

建议的未来结构：

```text
workflow/agents/
├── base.py
├── schemas.py
├── router.py
├── policy.py
├── sessions.py
├── claude_code.py
└── langgraph/
    ├── graph.py
    ├── state.py
    ├── nodes.py
    ├── tools.py
    ├── policies.py
    ├── checkpoints.py
    └── providers/
        ├── base.py
        ├── anthropic.py
        ├── openai.py
        ├── openai_compatible.py
        ├── internal_gateway.py
        └── local.py
```

### 迁移顺序

```text
Claude Code active
  → LangGraph shadow
  → 基于相同冻结 Artifact 比较结构化建议和实测结果
  → LangGraph optional main Analysis Agent runtime
  → Claude Code fallback
  → 达到独立退出标准后再讨论移除 Claude Code runtime 依赖
```

shadow 阶段不得执行或提交候选 revision。它只生成 `analysis_result` 和 `shadow_comparison` Artifact，由离线评估比较：Top-1/Top-3 算子命中率、非法实验数、Schema 失败率、实测恢复成功率、分析轮数、延迟、token、成本和跨模型一致性。

### Provider 准入

每个 provider/model 必须声明 capability profile，至少包括：

```yaml
capabilities:
  tool_calling: true
  parallel_tool_calling: false
  structured_output: json_schema
  streaming: true
  max_context_tokens: 131072
limits:
  max_output_tokens: 8192
  request_timeout_seconds: 900
  max_tool_rounds: 12
```

能力不足时只能执行显式降级、provider fallback 或返回 `unresolved`；不得把不支持的工具调用、结构化输出或上下文能力假定为可用。

### 独立切换条件

- shadow 输入与 active runtime 使用完全相同的 Artifact digest；
- LangGraph 输出通过当前 Analysis Result schema 和 policy validator；
- 非法实验率、Schema 失败率和 unresolved 率在预设范围内；
- 关键故障集合完成实测 suggest–verify–commit 回放；
- checkpoint、超时、provider fallback 和成本统计可审计；
- 切换主 Analysis Agent 运行时不修改任何确定性 Gate 或 release contract。

---

## 14. 完成定义

全部满足后本轮重构才算完成：

1. 唯一可执行主入口是 Plugin-only V3/V4；
2. 不运行本地 V1，不做 V1 三选；
3. 不路由 `gems_tree` 或 native pipeline；
4. V3 初始算子来自本轮全组件新生成且通过 freshness/identity 校验的 runtime oplist；无新 oplist 时的受限诊断例外只能驱动验证实验，不能替代官方发现集合；
5. 启动崩溃通过 operator revision 迭代处理；
6. V3 精度只对比 external NV，且 fail-closed；
7. V3 performance 只生成绝对 measurement Artifact；
8. V3 establishment 不包含性能；
9. V4 从不可变 `v3-final` 和 V3 performance 派生；
10. V4 无合法提升时回退 V3，不发布伪 V4；
11. Context、Artifact、Gate、revision、job、Agent Session、verification experiment 和 release decision 均有 schema；
12. Workflow Engine 只通过 `AnalysisAgent` 接口调用 Claude Code，shell 和业务 reducer 不直接依赖 Claude Code 文本；
13. 所有 Agent 建议经过 schema、identity、policy 和实测 suggest–verify–commit，不能直接写 committed revision、Gate 或 release decision；
14. Agent 不可用、输出非法或证据不足时显式进入 `unresolved`/manual handoff，不绕过 Gate；
15. 恢复不依赖历史文件名、完整对话或 Agent 自然语言，并支持 Agent Session/checkpoint 恢复；
16. ledger、trace、timing、report、batch 和 notifications 全部迁移，并区分假设与已验证事实；
17. `CLAUDE.md` 和 SKILL 文档与代码一致；
18. 旧双 pipeline、V1/V2 和独立 Plugin 流程已安全删除；
19. 成功、失败、Agent policy、verification rollback 和中断恢复测试全部通过；
20. LangGraph/ModelProvider/shadow 被保留为后续独立阶段，不阻塞本轮完成，也不侵入确定性 Engine。

完成后的职责边界为：

```text
V3：验证完整 FlagOS 环境可启动、精度合格，并记录真实性能。
V4：在冻结 V3 上追求性能提升，同时继续满足同一 external NV 精度红线。
Workflow Engine：拥有执行、证据、状态、Gate、revision 和发布决定。
Analysis Agent：分析复杂故障并提出受限、可验证的实验假设。
```
