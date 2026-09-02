# FlagOS Plugin-only 工作流优化方案

> 状态：流程设计已确认，待分阶段实现  
> 适用分支：`workflow-refactor`  
> 更新时间：2026-09-01

## 1. 文档目的

本文档记录 `workflow-refactor` 分支当前确认的流程优化方案。

本轮优化聚焦于已经安装完整 FlagOS 组件的准入镜像，删除旧流程中为构造本地基线、切换组件组合和比较原生性能而引入的复杂分支，将主流程收敛为 V3 精度交付与 V4 性能优化两部分。

本文档描述目标业务语义和流程边界。具体脚本、Context Schema、Artifact、Gate、恢复和发布模块的迁移应以此为依据。

---

## 2. 当前准入环境

当前流程只接受已经安装以下组件的镜像：

```text
vLLM + FlagGems + FlagTree + vllm-plugin-FL
```

对应的目标运行方式为：

```bash
VLLM_PLUGINS=fl
USE_FLAGGEMS=1
```

完整 FlagOS 组件环境本身就是当前流程的被测对象和交付对象。

本轮不再执行以下操作：

- 构造或恢复本地原生 vLLM 基线；
- 运行本地 V1；
- 执行 V1.1、V1.2、V1.3 三选；
- 在 `gems+tree` 与 `gems+tree+plugin` 两条 pipeline 之间分流；
- 在非 Plugin 与 Plugin 服务模式之间切换；
- 关闭 FlagGems、FlagTree 或 Plugin 组件以逼近原生环境；
- 通过本地 V1 性能结果判断 V3 是否达标。

组件必须保持安装和启用。为解决服务启动兼容性或满足精度红线，流程仍可调整 FlagGems 的具体算子替换集合。

---

## 3. 版本定义

### 3.1 V3：完整 FlagOS 交付版本

V2 与 V3 不再作为两个独立阶段存在。主流程产物统一称为 **V3**，不额外引入 `Primary` 等中间版本概念。

V3 的定义为：

```text
FlagGems + FlagTree + Plugin 全组件环境
+ 服务可以稳定启动
+ 精度满足外部 NV 基线红线
+ 记录该环境的绝对性能结果
```

V3 性能只测量和报告，不参与 V3 是否成立的判定。

### 3.2 V4：V3 之上的性能优化版本

V4 从已经冻结的 V3 最终算子配置出发，保持现有算子缩减和性能搜索逻辑：

```text
冻结的 V3 配置
  → 搜索性能更优的算子组合
  → 按性能结果进行精度回溯
  → 最终精度终检
  → 条件发布 V4
```

V4 的性能比较对象是 V3 实测性能，不是本地 Native/V1，也不需要外部性能基线。

---

## 4. 唯一业务红线：外部 NV 精度基线

当前无法获得真实的本地原生基线。旧流程中使用的 V1 精度基线，本质上也来自外部注入的 NV 评测结果。

新流程应直接将其建模为外部精度参考，不再包装为本地 V1：

```yaml
references:
  accuracy:
    source: external_nv
    model: ""
    datasets: {}
    max_relative_drop: 0.05
```

每个数据集独立计算相对退化：

```text
relative_drop =
    (nv_reference_score - v3_score)
    / nv_reference_score
```

判定条件为：

```text
relative_drop <= 5%
```

多数据集场景下，所有指定数据集均达标，V3 精度才达标。

外部 NV 精度基线是当前流程唯一的业务质量红线。性能和其他指标均不阻塞流程。

如果匹配当前模型、数据集和评测口径的外部 NV 基线缺失或不可解析，应采用 fail-closed 语义：

```yaml
accuracy:
  evaluation_completed: true
  qualification_status: unassessed
  qualified: false
  reason: reference_missing_or_invalid
```

不得将“评测命令执行成功”等同于“精度达标”。

---

## 5. 首次全组件启动与官方算子发现

### 5.1 首次启动前不预置算子白名单

当前模型的官方初始算子替换集合，必须由全组件服务运行时产生。

第一次启动服务时应使用：

```bash
VLLM_PLUGINS=fl
USE_FLAGGEMS=1
```

并且不预置当前模型的算子白名单。Plugin 在模型加载和算子匹配过程中生成：

```text
flaggems_enable_oplist.txt
```

该文件是当前模型官方认证的算子替换记录，也是后续启动兼容性调优和精度调优的权威集合来源。

首次启动前可以执行环境清理，但不能提前构造当前模型的算子配置：

1. 停止遗留推理服务并释放 GPU；
2. 清理 Triton 和 FlagGems 编译缓存；
3. 删除上一轮残留的运行时 oplist；
4. 清理上一轮持久化的算子白名单；
5. 记录本轮服务启动 ID 和开始时间；
6. 使用完整组件模式启动服务。

### 5.2 验证 oplist 属于本轮运行

不能只通过“文件存在”判断 oplist 有效。至少应验证：

- 启动前旧文件已被删除或归档；
- 文件在本轮启动后重新生成；
- 文件修改时间晚于本轮服务启动时间；
- 文件内容可以正常解析；
- 文件与本轮模型、镜像和服务启动 ID 关联；
- 原始文件被保存为不可变 Artifact。

建议产出：

```text
results/operator-configs/v3-discovered-oplist.txt
results/operator-configs/v3-discovered.json
```

---

## 6. V3 启动兼容性算子调优

第一次以完整组件模式启动时，可能因特定 FlagGems 替换算子发生编译错误、设备异常、图捕获错误或运行时崩溃。

因此首次服务启动不是一次性 Gate，而是“官方算子发现 + 启动兼容性调优”的迭代阶段。

### 6.1 基本循环

```text
VLLM_PLUGINS=fl + USE_FLAGGEMS=1
不预置白名单
        ↓
首次全组件启动并生成运行时 oplist
        ↓
服务是否就绪？
  ├─ 是：冻结启动稳定算子集合
  └─ 否：分析崩溃证据
             ↓
          定位问题算子
             ↓
          累计禁用问题算子
             ↓
          清理缓存并重新启动
             ↓
          核验新运行时 oplist
             ↓
          服务就绪或诊断穷尽
```

### 6.2 崩溃诊断优先级

发生启动崩溃时，禁用问题算子是最高优先解决方式。诊断顺序为：

1. 读取本轮服务日志；
2. 检查本轮新生成的 `flaggems_enable_oplist.txt`；
3. 运行 `diagnose_ops.py`；
4. 优先处理高置信度 `crashed_ops`；
5. 再分析低置信度 `candidate_ops`；
6. 检查 traceback 中的 `flag_gems` 调用路径；
7. 检查崩溃前最后编译或运行的 kernel；
8. 确定性证据仍不足、互相矛盾或需要跨日志归因时，调用当前 `ClaudeCodeAnalysisAgent`；
9. Analysis Agent 返回结构化假设和受限验证实验，不直接修改算子配置；
10. Workflow Engine 校验建议后创建实验性子 revision，执行清缓存、重启和运行时 oplist 核验；
11. 只有实验结果验证了问题消失，才提交新的启动 revision 并累计禁用该算子；
12. 有新的可归因算子时继续循环。

在穷尽确定性工具和日志证据前，不应通过关闭 Plugin、关闭 FlagGems、切换 Native 或添加未定义的 vLLM 启动参数规避问题。Analysis Agent 的结论始终是待验证假设，不能替代服务实测证据。

### 6.3 两类算子集合

必须同时保存以下两类集合，不能用调优后的集合覆盖原始发现证据。

#### 官方发现集合

```text
v3-discovered
```

表示 Plugin 在完整组件模式下为当前模型发现的初始候选替换算子。

#### 启动稳定集合

```text
v3-startup-stable
```

表示在官方发现集合中排除启动问题算子后，能够稳定启动服务的集合：

```text
v3-startup-stable
=
v3-discovered - startup_disabled_ops
```

精度评测从 `v3-startup-stable` 开始。

### 6.4 oplist 未完整生成时

启动崩溃可能发生在 oplist 生成完成之前，应区分处理：

- **已生成完整的新 oplist**：保存官方发现集合，并从该集合开始定位和禁用问题算子；
- **已生成但完整性不确定**：结合 Plugin 初始化日志、写入时序和算子数量判断，不得直接视为完整集合；
- **未生成新 oplist**：禁止使用旧文件或凭空构造官方集合，应继续根据 traceback、kernel 和诊断工具定位问题；实验候选只能来自本轮直接证据，并由本地代码核验其属于当前已安装 FlagGems 版本的已知算子目录。该目录仅用于验证“算子名和实验是否合法”，不能替代 `v3-discovered`；无法归因时记录为 Plugin 启动阶段未归因故障。

因此，Agent 建议中的算子通常必须属于当前 discovered/runtime 集合；仅当本轮 oplist 尚未生成时，才允许使用“本轮直接日志证据 + 当前安装版本已知算子目录”的受限例外。服务恢复后仍必须以新生成且通过 freshness/identity 校验的运行时 oplist 建立官方发现集合。

### 6.5 启动调优产物

该阶段必须独立记录：

- 原始官方发现集合；
- 每轮启动配置 revision；
- 每轮禁用的算子及证据；
- 每轮服务日志；
- 最终启动稳定集合；
- 实际运行时 oplist；
- ledger、trace、issue log 和恢复点。

---

## 7. V3 精度评测与精度算子调优

服务基于 `v3-startup-stable` 成功启动后，执行指定数据集的精度评测，并与外部 NV 精度基线逐数据集比较。

### 7.1 精度首次达标

如果所有数据集均满足精度红线：

```text
v3-startup-stable → v3-final
```

无需执行精度算子调优。

### 7.2 精度不达标

如果任一数据集相对外部 NV 基线退化超过阈值，自动进入精度算子调优：

```text
v3-startup-stable
  → 定位精度问题算子
  → 累计禁用
  → 重启服务并核验运行时 oplist
  → 重新评测精度
  → 达标或达到调优上限
```

精度调优只能在 `v3-startup-stable` 范围内继续减少算子，不能引入 `v3-discovered` 之外的替换算子。

启动阶段和精度阶段的禁用原因必须分开保存：

```yaml
disabled_ops:
  startup: []
  accuracy: []
```

最终 V3 配置为：

```text
v3-final
=
v3-discovered
- startup_disabled_ops
- accuracy_disabled_ops
```

### 7.3 精度回归分析中的 Agent 参与

精度调优优先使用可重复的分组排查、逐轮评测和已有算子搜索工具。当结果异常、候选算子难以排序、不同数据集证据冲突，或确定性搜索无法在预算内收敛时，Workflow Engine 可以调用 Analysis Agent：

- 输入仅包含当前 run、数据集、算子 revision 和已注册 Artifact；
- Agent 可以归纳错误模式、排序候选算子并提出下一项受限实验；
- Agent 不得直接写入 `disabled_ops.accuracy`，也不得声明精度达标；
- 每项建议必须由本地策略校验，并通过新子 revision、服务重启和完整评测验证；
- 最终精度结论只由外部 NV 参考、评测 Artifact 和确定性 Gate Reducer 产生。

发布前必须对 `v3-final` 对应的全部数据集执行最终精度验证。

---

## 8. V3 性能测试

V3 性能阶段只测量完整 FlagOS 环境的实际表现：

```text
运行 benchmark
→ 保存绝对性能指标
→ 写入报告
```

不执行：

- 本地 Native/V1 性能基线测试；
- 合成 Native 性能基线；
- V3 与 Native/V1 的性能比较；
- V3 性能 ratio 达标判定；
- 因性能数值触发的 V3 算子性能调优；
- 因性能不佳阻塞发布或后续流程。

建议记录：

```yaml
v3:
  performance:
    execution_status: succeeded
    measured: true
    result_artifact: results/v3_performance.json
    metrics:
      request_throughput: 0
      output_throughput: 0
      total_throughput: 0
      mean_ttft_ms: 0
      p99_ttft_ms: 0
      mean_tpot_ms: 0
      p99_tpot_ms: 0
```

性能工具执行失败属于步骤执行异常，应记录和按工具失败策略处理；性能数值高低不构成业务失败。

---

## 9. V3 成立与发布

V3 的成立条件为：

```text
v3_established =
    service_ready
    AND final_accuracy_qualified
    AND runtime_operator_config_verified
```

性能不参与 V3 成立条件。

发布前冻结：

```text
v3-final 算子配置
+ 最终运行时 oplist
+ 外部 NV 精度比较结果
+ V3 绝对性能结果
+ 镜像、模型和组件版本信息
```

精度低于外部 NV 红线时，流程仍应完成结果保存、问题记录、报告生成和既定的私有诊断发布收尾，但不得将该版本标记为精度达标的正式 V3。

---

## 10. V4 性能优化

V4 保持现有性能优化职责，从不可变的 `v3-final` 派生，不修改 V3 本身。

### 10.1 基本流程

```text
冻结的 v3-final
   ↓
V4 阶段一：性能搜索
   - 逐步尝试减少算子
   - 只在性能提升时推进当前最优基线
   - 保留每个候选组合和性能结果
   ↓
V4 阶段二：精度回溯
   - 按性能从高到低验证候选组合
   - 对比同一份外部 NV 精度基线
   - 找到精度达标的最高性能组合
   ↓
V4 最终精度终检
   ↓
条件发布 V4
```

### 10.2 V4 成立条件

```text
v4_established =
    search_execution_success
    AND performance_improved_over_v3
    AND accuracy_qualified_against_external_nv
    AND retained_operator_count >= 1
    AND runtime_operator_config_verified
```

如果搜索正常完成但没有找到同时满足性能提升和精度要求的组合：

```yaml
v4:
  execution_success: true
  established: false
  fallback_to_v3: true
  reason: no_valid_improvement
```

此时整个流程仍然成功，最终交付保持 V3，不发布与 V3 等价的伪 V4。

---

## 11. 算子配置生命周期

建议将算子配置保存为不可变 revision：

```text
v3-discovered       Plugin 官方发现集合
       ↓
v3-startup-r1       第一次启动问题算子禁用
       ↓
v3-startup-rN       后续启动问题算子累计禁用
       ↓
v3-startup-stable   服务稳定启动集合
       ↓
v3-accuracy-r1      第一次精度问题算子禁用
       ↓
v3-accuracy-rN      后续精度问题算子累计禁用
       ↓
v3-final            最终精度验证通过并冻结
       ↓
v4-r0               从 v3-final 克隆
       ↓
v4-rN               V4 性能搜索候选
       ↓
v4-final            性能提升且精度达标的最终候选
```

每个 revision 至少记录：

```yaml
operator_config:
  id: v3-startup-r1
  parent_id: v3-discovered
  candidate: v3
  source: startup_tuning

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
    service_ready: false
    runtime_oplist_matches: false

  evidence: []
  created_at: ""
  artifact_path: ""
```

运行时生成的 oplist 是实际生效证据，配置文件中的白名单是控制输入，两者必须同时保存并核验差异。

---

## 12. 目标端到端流程

```text
01 容器和工作目录准备
   ↓
02 FlagOS 全组件准入检查
   ↓
03 清理旧运行状态、旧 oplist 和编译缓存
   ↓
04 V3 全组件首次启动与官方算子发现
   - VLLM_PLUGINS=fl
   - USE_FLAGGEMS=1
   - 不预置当前模型白名单
   ↓
05 V3 启动兼容性算子调优
   - 崩溃时定位问题算子
   - 累计禁用并重新启动
   - 直到服务就绪或诊断穷尽
   ↓
06 冻结 V3 启动稳定算子集合
   - 保存官方发现集合
   - 保存启动禁用列表
   - 核验实际运行时 oplist
   ↓
07 V3 精度评测
   - 对比外部 NV 精度基线
   ↓
08 V3 精度算子调优（条件触发）
   ↓
09 V3 最终精度验证
   ↓
10 V3 性能测试
   - 只记录绝对结果
   - 不比较、不调优、不门控
   ↓
11 冻结并发布 V3
   ↓
12 V4 性能优化
   - 从 v3-final 派生
   ↓
13 V4 精度回溯和最终验证
   ↓
14 V4 条件发布
   ↓
15 最终报告、状态回传和资源清理
```

---

## 13. 状态与门控原则

执行状态和业务结论必须分开：

```yaml
steps:
  v3_accuracy:
    execution_status: succeeded

gates:
  v3_accuracy:
    qualification_status: failed
```

例如：

- 精度评测命令成功，但相对退化超过 5%：步骤执行成功，精度 Gate 失败；
- 性能 benchmark 成功，但吞吐较低：步骤执行成功，性能结果仅记录；
- V4 搜索完成，但没有合法性能提升：步骤执行成功，V4 不成立；
- Artifact 损坏或无法解析：Gate 必须失败，禁止异常后放行。

关键状态建议为：

```yaml
gates:
  admission_passed: false
  v3_service_ready: false
  v3_accuracy_qualified: false
  v3_operator_config_verified: false
  v3_established: false
  v3_release_eligible: false
  v4_execution_success: false
  v4_established: false
```

不再使用包含性能门槛的统一 `qualified` 字段控制主流程。

---

## 14. 本轮明确删除的旧语义

实现新流程并完成迁移验证后，逐步删除：

- `gems_tree` Branch A；
- native pipeline；
- 本地 V1 服务、精度和性能阶段；
- V1.1、V1.2、V1.3 baseline selector；
- V2.1、V2.2、V3.1、V3.2 分支；
- V2 与 V3 双 Tag 补偿逻辑；
- 独立 Plugin 步骤 9–13；
- `plugin_workflow.*` 状态；
- V1/V2 性能对比；
- 合成 Native 性能基线；
- V3 性能达标门控；
- V3 性能算子调优；
- 以 `performance_ok` 阻塞或标记 V3 的逻辑；
- 旧的 V1/V2/V3 对比型报告章节和恢复文件名。

旧代码应在新状态、Gate、恢复、发布和报告路径可用之后删除，避免先删除再通过临时兼容逻辑补洞。

---

## 15. 未来 V0 扩展边界

后续可能新增一个独立的 V0 前置场景：

```text
纯 vLLM 环境
  → 测试精度和性能
  → 自动安装 FlagGems + FlagTree + Plugin
  → 切换到完整 FlagOS 环境
  → 继续本文定义的 V3/V4 流程
```

V0 当前不纳入本轮设计和实现，不应增加当前 Plugin-only 主流程的复杂度。

当前 Workflow Engine 和状态模型只需避免阻断未来增加前置阶段，不需要预先引入 V0 字段、V0 Gate 或组件安装编排。

---

## 16. Analysis Agent 运行架构

### 16.1 本轮定位

本轮重构允许关键复杂分析节点继续依赖 Claude Code，但这种依赖必须被限制在可替换的分析层中：

```text
Deterministic Workflow Engine
    ├── 执行服务、评测、benchmark、发布和恢复
    ├── 注册并校验 Artifact
    ├── 创建不可变 operator revision
    ├── 计算 Gate 和推进状态
    └── 在证据不足时调用 AnalysisAgent
            └── ClaudeCodeAnalysisAgent（本轮实现）
```

Claude Code 是第一代 **Analysis Agent Runtime**，不是工作流状态机，也不是业务 Gate 的裁决者。它可参与：

- 复杂启动崩溃归因；
- 确定性精度排查无法收敛后的回归分析；
- 未知或互相矛盾的运行时故障分析；
- 候选问题算子的排序；
- 受限验证实验的建议。

### 16.2 不可越权边界

任何 Analysis Agent 均不得：

- 直接修改当前 operator revision；
- 永久禁用未经实测验证的算子；
- 写入 `accuracy.qualified=true`、`v3.established=true` 或 `v4.established=true`；
- 校验或修复外部 NV 参考；
- 修改 Artifact 哈希、身份或有效性；
- 在证据缺失时放行 Gate；
- 为 V3 创建性能达标结论；
- 根据自然语言结论选择发布版本。

### 16.3 统一接口与结构化契约

Workflow Engine 只依赖 provider-neutral 接口：

```python
class AnalysisAgent:
    def analyze_startup_failure(self, request: StartupFailureRequest) -> AnalysisResult:
        ...

    def analyze_accuracy_regression(self, request: AccuracyRegressionRequest) -> AnalysisResult:
        ...

    def analyze_unknown_failure(self, request: UnknownFailureRequest) -> AnalysisResult:
        ...
```

本轮实现：

```python
class ClaudeCodeAnalysisAgent(AnalysisAgent):
    ...
```

输入请求必须绑定 `workflow_run_id`、candidate、当前 operator revision 和输入 Artifact。输出必须通过 JSON Schema 校验，例如：

```json
{
  "schema_version": "1.0",
  "analysis_type": "startup_failure",
  "status": "hypothesis_available",
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
  }
}
```

本地代码还必须校验：算子属于当前发现集合、run 和 revision 身份一致、引用证据可访问、建议实验在策略白名单内。

### 16.4 suggest–verify–commit

Agent 参与统一遵循：

```text
Agent 读取受限证据
  → 提出结构化假设
  → Schema 与策略校验
  → Engine 创建实验性子 revision
  → 确定性工具执行实验
  → 生成实测 Artifact
  → Reducer 判断假设是否成立
  → 成功则提交 revision，失败则回滚并记录负证据
```

Agent 建议本身只形成 `analysis_result` Artifact。只有验证实验形成的 `verification_experiment` Artifact 才能推动 revision 提交。

### 16.5 Agent 会话、恢复与不可用行为

Context 只保存 Agent 会话引用，不嵌入完整对话：

```yaml
agent_sessions:
  agent-session-001:
    task_type: startup_diagnosis
    workflow_run_id: run-001
    candidate: v3
    operator_revision: v3-startup-r2
    status: running
    runtime: claude_code
    model_provider: claude_code
    checkpoint_id: ""
    input_artifacts: []
    output_artifacts: []
```

Agent 不可用时：

1. 可独立执行的确定性步骤继续运行；
2. 已知本地诊断路径继续执行；
3. 必须进行复杂分析而 Agent 不可用时，将会话标记为 `unresolved`；
4. 生成诊断、issue 和 manual-handoff Artifact；
5. 不伪造结论、不绕过 Gate。

### 16.6 未来 LangGraph 迁移

后续阶段以固定 LangGraph harness 替换 Claude Code 分析运行时：

```text
Deterministic Workflow Engine
    └── AnalysisAgent
          ├── ClaudeCodeAnalysisAgent（现有/回退）
          └── LangGraphAnalysisAgent（未来主实现）
                 └── ModelProvider
                       ├── AnthropicProvider
                       ├── OpenAIProvider
                       ├── OpenAICompatibleProvider
                       ├── InternalGatewayProvider
                       └── LocalModelProvider
```

LangGraph 只替换分析 harness，不替换本文 15 步确定性 Workflow Engine。未来 Agent 使用受限领域工具，例如：

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

每个模型通过 capability profile 声明结构化输出、tool calling、上下文、超时和最大工具轮数。能力不足时降级到 suggestion-only、受校验文本 JSON、其他 provider 回退或 `unresolved`，不得假设任意模型能力等价。

迁移采用 shadow 模式：Claude Code 与 LangGraph 对相同 Artifact 独立分析，但 shadow 结果不控制工作流。比较 Top-1/Top-3 算子命中率、无效实验数、恢复成功率、Schema 失败率、分析轮数、延迟、token、成本和跨模型一致性后，再逐步将 LangGraph 提升为可选主实现，Claude Code 保留为回退。

---

## 17. 实施顺序建议

建议按以下顺序实施，避免一次性删除导致流程不可运行：

1. 固化本文档中的版本、精度、性能和算子集合语义；
2. 建立新 Context Schema、Artifact 契约和 fail-closed Gate；
3. 建立确定性 Workflow Engine、恢复机制和不可变 revision；
4. 定义 `AnalysisAgent`、结构化请求/结果、策略校验和 Agent Session；
5. 实现 `ClaudeCodeAnalysisAgent`，接入启动、精度和未知故障的关键分析节点；
6. 实现 Plugin-only 准入；
7. 实现 V3 首次启动、官方算子发现和启动兼容性调优；
8. 迁移 V3 精度评测和精度算子调优；
9. 将 V3 性能阶段改为纯测量；
10. 迁移 V3 发布、恢复和报告；
11. 将 V4 明确绑定到冻结的 `v3-final`；
12. 增加状态转换、Artifact、Agent policy、恢复和算子 revision 测试；
13. 最后删除旧双 pipeline、V1、V2 和 Plugin 附加流程代码；
14. 在后续独立阶段实现 LangGraph、ModelProvider 和 shadow 迁移，不阻塞本轮交付。

---

## 18. 最终原则

本轮流程优化遵循以下原则：

1. **完整 FlagOS 组件环境就是被测环境，不通过关闭组件逼近原生环境。**
2. **当前模型的初始替换算子必须来自全组件首次启动生成的运行时 oplist。**
3. **首次启动崩溃必须先做启动兼容性算子调优，不能直接判定流程失败。**
4. **启动调优、精度调优和 V4 性能优化是三个独立阶段，禁用原因分别记录、配置逐步累计。**
5. **外部 NV 精度基线是唯一业务红线。**
6. **V3 性能只测量和报告，不比较、不调优、不门控。**
7. **V3 是完整组件主交付版本，V4 是从冻结 V3 派生的性能优化版本。**
8. **V4 未成立不影响 V3 交付，也不得发布与 V3 等价的伪 V4。**
9. **确定性 Workflow Engine 拥有执行、状态、Artifact、Gate、revision 与发布决定。**
10. **Claude Code 是本轮可替换的第一代 Analysis Agent Runtime，只提出可验证假设。**
11. **所有 Agent 变更均遵循 suggest–verify–commit，模型文本不能直接成为业务事实。**
12. **LangGraph 未来只替换分析 harness，并通过 ModelProvider 接入不同模型 API。**
13. **Agent 不可用时显式进入 unresolved/manual handoff，绝不绕过 Gate。**
14. **未来 V0 是独立的前置扩展，本轮不实现。**
