# FlagOS Plugin-only 工作流重构 — 项目交接文档

> **维护分支**: `workflow-refactor` · **更新日期**: 2026-09-07
> **一句话**: 把旧编排器里的 V1/V2 双轮层剥掉、精度判定固化成脚本退出码(本轮已做);更彻底的"确定性引擎 + Claude 仅当受约束分析器"三层架构(`workflow/`)底座已就位、有单测,但**执行调度与 Claude 接线仍是占位,尚未接管真实流程**(核心待办)。

---

## 1. 前因 — 为什么做这件事

触发问题:**「workflow-refactor 分支的日志里为什么还有 V1/V2」**。

根因:`prompts/run_pipeline.sh`(约 3400 行)是**旧编排器**,处于**半迁移态**——

- **段2(steps 4-7)** 仍跑经典 **V1(native) + V2(FlagGems) 双轮**精度/性能路径,判定用本地 V1 基线、性能有 step7 强制闸门、无 V1 时合成基线 ×1.05、准入用 baseline 三选状态机。
- **段4(SEG4/SEG4V4)** 才是 plugin-only 的 V3→V4。

即:旧 V1/V2 + 后接 plugin V3/V4 混在一个 shell 里。

方向诉求(见 memory `determinism-in-scripts`、`plugin-only-primary-workflow`):**工作流固定化 + 减少 Claude Code 参与**——能脚本化的确定性逻辑必须脚本化,Claude 只做编排和复杂报错分析,最终目标是 Claude 退出编排、只当受约束的分析器。

---

## 2. 工作全景 — 三条推进线

| 线 | 载体 | 目标 | 状态 |
|----|------|------|------|
| **A. shell 内剥离** | `prompts/run_pipeline.sh` + `accuracy_compare.py` | 在旧编排器里去掉 V1/V2 双轮,把精度判定固化到脚本退出码 | **本轮主要工作,A–I 改写已完成并 `bash -n` 通过** |
| **B. 确定性引擎** | `workflow/` Python 包(三层) | Claude 退出编排,Engine 驱动 15 步,Claude 仅当 Analysis Agent | **底座+协议就位、有单测;执行调度与 Claude 接线是占位** |
| **C. 死代码物理删除** | `step7_gate.py` / `baseline_selector.py` / `synthesize_perf_baseline.py` 等 | 迁移完成后清理 | **未开始(plan §J,归最后独立收尾轮)** |

---

## 3. 本轮已完成(线 A)

对齐审批方案 `/home/lz/.claude/plans/silly-kindling-flurry.md`(决策 B+A):

1. **精度判定固化**(决策 B):重写 `skills/flagos-eval-comprehensive/tools/accuracy_compare.py`
   - 新契约 `--reference/--candidate`(外部 NV 参考 vs 候选),输出 `aligned/rel_drop/missing_nv` 结构化字段
   - 退出码语义化:`0`=达标 · `1`=不达标 · `2`=参数/文件错 · `3`=缺 NV 基线(fail-closed,编排层兜底)
   - 小样本噪声判定 `_noise_zone_check` 进脚本(`NOISE_ABS_QUESTIONS=2.0`),**取消历史退出码 4**(曾被 agent 误解为要跑全量复测)
2. **段2 双轮→单轮**(决策 A):`run_pipeline.sh` 段2 改为 plugin-only 单轮 V3(准入 → V3 起服务 → V3 精度 vs 外部 NV → V3 性能仅测量不设 Gate → V4)
3. **删除分支判断**:native 准入层 fail-closed 拒绝、`IS_NATIVE` 分支、step7 性能强制闸门、合成基线 ×1.05、baseline 三选状态机;段3 不再对外发布,V3 归并段4 作唯一交付(私有 `flagrelease-project`)
4. **文案/注释 V1/V2→V3/V4 清理**,grep 确认剩余 V1/V2 仅为"说明其不存在/已并入"的描述或历史注释

> 新旧流程对比图见 `docs/workflow_comparison.png`(生成脚本 `docs/workflow_comparison.py`)。

---

## 4. 核心差距 — `workflow/` 三层引擎接线状态(重点)

`workflow/` 按"确定性引擎 + 受限分析器"设计,分三层。**目前只有最底下的 CLI 助手真正被 shell 调用**(`run_pipeline.sh:743` → `workflow/cli/generate_comparison_and_config.py`),上面两层已成型但没接线。

| 层 | 组成 | 状态 | 差的接入点(文件:行) |
|----|------|------|--------------------|
| **CLI 助手** | `cli/generate_comparison_and_config.py` | ✅ 已接线(shell 743 调用) | 无 |
| **Engine 骨架** | `engine/workflow_engine.py`、`recovery.py`、`operator_revision_store.py`、`verification_executor.py` | 🟡 状态机/context/revision/恢复点逻辑真实,`operator_revision_store` 有单测 | **① `workflow_engine.py:333` `execute_step` 是注释、`:336` `run()` 主循环直接 `break`** |
| **Domain 执行器** | `domain/` 下 8 个 | 🟡 `admission`、`v3_startup` 有单测;多处占位 | **② `v3_performance.py:127` 占位"由 Engine 注入执行器"**<br>**③ `v3_accuracy.py:196` `accuracy=65.2` 模拟值**<br>**④ `v3_accuracy_tuning.py:171`/`v3_startup_tuning.py:165` "暂时 break",未接续轮**<br>**⑤ `v3_release.py:160/169/191`、`v4_release.py:173/190`、`v4_reduction.py:266` `success=True # 占位`** |
| **Analysis Agent** | `agent/protocol.py`、`claude_code_agent.py`、`policy_validator.py`、`session_manager.py` | 🟡 协议契约真实 | **⑥ `claude_code_agent.py:421-435` "adapter not yet implemented, returning mock result"** |
| **底座** | `schemas/context_v2.py`、`artifacts/registry.py`、`gates/reducer.py` | ✅ 逻辑真实且有单测 | `workflow_engine.py:83` `_load_or_initialize_context` 现假设 JSON,需接 `context.yaml` YAML 序列化 |

### 4.1 "减少 Claude 参与"的架构机制(`agent/protocol.py`)

终态里 Claude 不再自由编排,而是被结构化契约框死在窄窗口:

- **只在两类分析场景被唤起**:`StartupFailureRequest`(启动失败)、`AccuracyRegressionRequest`(精度退化);其余步骤全走确定性 Engine。
- **输入输出是 schema 不是自由对话**:请求带 `input_artifacts`(已登记证据)、`operator_constraints`(如 `discovered_set` 官方算子集)。
- **行为空间硬限额**:`limits` 里 `max_candidate_ops:3`、`max_tool_rounds:12`、`timeout_seconds:900` 全是常量。
- **可被非 Claude 替换**:协议注释明说还有 `LangGraphAnalysisAgent` 实现——Claude 只是当前默认分析器,非流程主干。
- `policy_validator.py` 对 Claude 返回的 `AnalysisResult` 做确定性校验(是否越界、是否用白名单外算子),给输出加护栏。

---

## 5. 接入点清单 — 还差什么(按依赖排序)

真正把流程从"shell+Claude 编排"切到"Engine 驱动 + Claude 仅分析",需依次补:

1. **`Engine.execute_step`**(①)——15 步 dispatch 到对应 domain 执行器,`run()` 去掉 `break`
2. **domain 执行器接真实 `docker exec`**(②③⑤)——`65.2` 模拟、`success=True # 占位` 换成真实评测/发布调用
3. **调优多轮循环**(④)——两个 tuning 执行器的 `break` 换成"续轮直到达标或到上限"
4. **`ClaudeCodeAnalysisAgent` 真接线**(⑥)——按 protocol 拉起 Claude Code,替换 mock
5. **context YAML 序列化**——`_load_or_initialize_context` 从"假设 JSON"接到真实 `context.yaml`
6. **shell 切换调用点**——`run_pipeline.sh` 从"Claude 逐段编排 + 743 调 CLI 助手"改为调 `WorkflowEngine.run()`,Claude 退到 Analysis Agent 位置

---

## 6. 认知校准 — status 文档的 ✅ ≠ 已接线

`docs/plugin_only_implementation_status.md` 把 Phase 1-4 标为 ✅ 完成、"38 tests 100% passing",但那反映的是**骨架逻辑 + 单测通过**,**不等于执行调度和真实落地已接线**。阅读时以本文件第 4/5 节的实际接线状态为准。两者差异正是本次交接要澄清的核心。

---

## 7. 文档索引(清理后保留)

| 文档 | 用途 |
|------|------|
| `docs/plugin_only_workflow_refactor_plan.md`(1720 行) | **核心设计方案**——完整重构蓝图 |
| `docs/plugin_only_workflow_optimization.md`(849 行) | 工作流规格/优化方案 |
| `docs/plugin_only_implementation_status.md` | 15 步实现进度跟踪(读时配合第 6 节校准) |
| `docs/step_14_langgraph_deferred.md` | LangGraph 迁移延后决策(呼应 agent 层可替换性) |
| `docs/workflow_comparison.png` / `.py` | 新旧流程对比图及生成脚本 |
| `docs/project_guide.md` / `SKILLS-OVERVIEW.md` | 框架说明书 / Skills 概览 |
| `docs/field_reference.md` / `report_template.md` | 产出字段参考 / 报告模板 |
| `docs/notification_and_result_analysis_design.md` | 通知与结果分析设计 |
| `/home/lz/.claude/plans/silly-kindling-flurry.md` | 本轮 A–J 改写审批清单 |

**本次已删除的中间过程文件**(报告数据契约修复线的 progress/summary/gap/planning、会话总结、已执行的 step13 计划、shell 集成指南)——tracked 文件可 `git checkout <sha> -- docs/<name>` 恢复;untracked 的 `complete_code_planning.md`、`report_data_contract_fix_complete.md`、`report_generation_data_contract_gap.md`、`shell_integration_guide.md` 已不可恢复。

---

## 8. 代码位置索引

| 关注点 | 位置 |
|--------|------|
| 旧编排器(shell) | `prompts/run_pipeline.sh` |
| shell → workflow 唯一接线 | `run_pipeline.sh:743` → `workflow/cli/generate_comparison_and_config.py` |
| 精度判定脚本(本轮重写) | `skills/flagos-eval-comprehensive/tools/accuracy_compare.py` |
| 确定性引擎骨架 | `workflow/engine/workflow_engine.py`(`run()`:300、15 步定义:44) |
| 15 步 domain 执行器 | `workflow/domain/*.py` |
| Analysis Agent 协议 | `workflow/agent/protocol.py`、`claude_code_agent.py` |
| 单测(38) | `workflow/tests/` |
| 环境准入分类 | `skills/flagos-pre-service-inspection/tools/inspect_env.py` |

---

## 9. 遗留待办 / 风险

- **待办**:第 5 节 6 个接入点;plan §J 死代码物理删除(`step7_gate.py`/`baseline_selector.py`/`synthesize_perf_baseline.py`/`prompts/auto_v1v2_pipeline.md` 等)+ `native_performance.json`/`gpqa_native.json`/`run_postprocessing "v2"` 内部标签命名清理(需同步改 `generate_comparison_and_config.py`)。
- **风险**:线 A(shell)与线 B(引擎)当前**并存**——真实流程仍走 shell,引擎未接管;切换(接入点 6)是高风险动作,需端到端验证。
- **未验证**:Step 15 端到端(真实容器跑通 15 步)尚未做。

---

## 10. 恢复 / 回滚锚点

- 归档分支:`archive/legacy-dual-pipeline`
- 清理前 tag:`legacy-code-before-cleanup`
- **分支纪律**:`refactor` 分支仅放基线数据,代码改动**不进** `refactor`;当前 `workflow-refactor` 是代码工作分支。任何 commit/push 需显式指令。
