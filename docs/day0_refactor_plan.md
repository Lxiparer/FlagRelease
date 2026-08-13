# day0 分支重构方案（v2 — 按用户 2026-08-13 纠正修订）

> 分支：`day0`（基于 `refactor` @ 84d2787）
> 状态：**已实施**（M1-M5 完成；M6 端到端待真实镜像+权重验证）
> 修订记录：v2 吸收 8 处纠正（私有发布含魔搭/HF、全量 gpqa_diamond、全组件镜像、冒烟排障顺序、修复自主权等）

---

## 1. 背景与目标

### 1.1 day0 场景定义（用户定稿口径）

| 维度 | 需求 |
|------|------|
| 输入 | 镜像地址 + **本地权重路径**（无需容器名识别/权重自动搜索）。**镜像默认必含 plugin + flaggems + flagtree 全组件** |
| 启动 | **flagos 全组件**启动模型服务（plugin + FlagGems 全量 + FlagTree） |
| 冒烟测例 | curl 提问"中国首都在哪"，**测例通过且服务不崩溃**才进入精度评测 |
| 精度 | 评测**全量 gpqa_diamond**（单数据集，不截断题数）；不达标 → **关算子调优直到达标** |
| 性能 | 精度有效后才测性能，**只采集数据**（无 V1 对比、无 80% 闸门、无性能调优） |
| 发布 | **私有发布**：Harbor + ModelScope + HuggingFace 三处**全部私有、不公开**（tag 从简，用 `-day0`） |
| 排障授权 | 启动失败/冒烟失败允许 Claude 自行排查：**优先关算子** → 探索 flagos 其它组件问题（eager 仅启动阶段可用）；修复环节**允许 Claude 自由发挥**，判断复杂度，轻量可修就修 |
| 失败兜底 | 无法修复 → 流程结束，**问题总结报告**（现象、原因、建议修复方案）；**修复后不同步上游**，报告即交付物 |
| 精度无效 | 精度**报错/无效且无法解决时跳过**（不做性能评测），产出问题总结报告 |

### 1.2 与现有 1-15 步流程的本质差异

| 差异点 | 现有流程 | day0 |
|--------|---------|------|
| 入口 | 容器名/镜像 + 模型名自动搜索权重；分支 A/B 路由（native/gems_tree/gems_tree_plugin） | **镜像 + 显式权重路径**；镜像**必为全组件（gems_tree_plugin）**，无分支路由 |
| 基线 | V1 本地基线（分支 B 三选状态机 / NV 兜底 / 合成性能基线） | **无 V1**，精度基线直接 NV（`nv_baseline.yaml`） |
| 精度数据集 | 默认 gpqa_diamond 截断（--limit 30/50）+ 可选多数据集 | **全量 gpqa_diamond**（不传 --limit），单数据集 |
| 性能 | V1/V2 对比 ratio ≥80% 判定 + 步骤7 性能调优 | **单轮采集**，无对比无闸门无调优 |
| 冒烟 | 仅步骤5"hello" curl 连通性检查 | **"中国首都在哪"问答判定为硬闸门**（通过才进评测） |
| 排障手段 | 关算子（第一原则）→ enforce-eager 仅最后辅助手段；**禁止改组件源码** | **关算子优先 → 组件探索 → 轻量源码修复（Claude 自主发挥）**；eager 仅启动阶段 |
| 失败兜底 | issue + 私有发布（流程不可终止，约束18） | **问题总结报告**（现象/原因/建议方案），流程合法终止；修复不同步上游 |
| 发布 | 多版本多 tag（V1-V4）；私有=仅 Harbor | 单一交付 tag `-day0`；**私有 = Harbor + MS + HF 全私有不公开** |

**结论：day0 是"全组件快速适配验证"场景。** 门控逻辑差异大，采用**独立编排 + 复用工具**策略，不侵入现有 `run_pipeline.sh` 稳定流程。

---

## 2. 现状盘点（可复用资产）

| 资产 | 位置 | day0 复用方式 |
|------|------|--------------|
| 编排模式（段式 prompt 构造、长任务协议、断点续跑、段间越界检测、幂等检查、GPU 清理） | `prompts/run_pipeline.sh` | **借鉴结构**，重写精简版 |
| 容器创建模板 / setup_workspace.sh 工具部署 | `skills/flagos-container-preparation/` | 直接复用（需支持 day0 模板选择） |
| inspect_env.py 场景分类 | `skills/flagos-pre-service-inspection/tools/` | 复用（仅采集信息；不再用于分支路由） |
| start_service.sh / wait_for_service.sh / safe_restart_service.sh | `skills/flagos-service-startup/tools/` | 直接复用（start_service.sh 需加 eager 选项） |
| **冒烟测例函数**（SMOKE_PROMPT="中国的首都是哪里？"，关键词"北京"） | `baseline_selector.py:283` `smoke_test()` | **抽离为独立 `smoke_test.py`**，双方共用 |
| fast_gpqa.py（evalscope）/ eval_wrapper.py / accuracy_compare.py（NV 模式，rel_drop≤5%） | `skills/flagos-eval-comprehensive/tools/` | 直接复用（去掉 V1/V2 对比与 --limit） |
| diagnose_ops.py / operator_search.py（精度调优，plugin 模式 env blacklist） | `skills/flagos-operator-replacement/tools/` | 直接复用（**算子控制固定走 plugin env 路径**，见 §4.1） |
| benchmark_runner.py | `skills/flagos-performance-testing/tools/` | 直接复用（单轮采集，无 performance_compare） |
| main.py 发布（Harbor + MS + HF，仓库默认私有） | `skills/flagos-release/tools/` | 直接复用（需加 `day0` tag 选项；**不用 --only-harbor**） |
| generate_report.py / chip_spec.py | `shared/` | 复用主报告；day0 问题总结报告独立新增 |
| update_context.py / task_runner.py | `shared/`、各 skill tools | 直接复用 |
| nv_baseline.yaml（82 模型 gpqa/mmlu/math_500） | `shared/` | 直接复用为精度基线 |

---

## 3. 总体设计

**原则**：
1. **不侵入现有稳定流程** —— `run_pipeline.sh`、现有 12 个 SKILL.md 编排指令、`context.template.yaml`、`generate_report.py` 均不改（仅 `baseline_selector.py` 做无行为变化的抽离小重构）。
2. **工具复用、编排独立** —— day0 新增文件集中在 3 处：一个编排脚本、一个 SKILL.md、一组 day0 专属状态/报告文件。
3. **报告即兜底交付物** —— 成功场景出常规报告（精度/性能/发布）；遇到问题（修复过/不可修复/精度无效）出**问题总结报告**。

### 3.1 编排：`prompts/run_day0.sh`（新增，~700 行）

```
用法: bash prompts/run_day0.sh <镜像地址> <本地权重路径> \
      <MODELSCOPE_TOKEN> <HF_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD> [--verbose]
```

（MS/HF token 必传——私有发布需建私有仓并上传权重）

段划分（3 段 + 报告收尾，借鉴 run_pipeline.sh 的段框架）：

```
段1 准备+启动+冒烟   步骤D1 容器准备（镜像+权重路径挂载→容器，模型名取权重路径 basename）
                    步骤D2 环境检测（inspect_env.py，仅采集信息；算子控制走 plugin env）
                    步骤D3 全组件启动（plugin + flaggems 全量 + flagtree）
                    步骤D4 首都冒烟测例（smoke_test.py，硬闸门）
                    [排障循环] 见 §4.1 决策树
段2 精度            步骤D5 精度评测（fast_gpqa.py --dataset gpqa_diamond 全量不截断，
                          基线=nv_baseline.yaml，accuracy_compare.py NV 模式 rel_drop≤5%）
                    步骤D6 精度调优（accuracy_ok=false 时 operator_search.py 关算子，≤3 轮）
                    [无效兜底] 报错/无效且无法解决 → 问题总结报告 → 跳过段3 → 结束
段3 性能+发布       步骤D7 性能评测（benchmark_runner.py 单轮采集，无闸门无调优）
                    步骤D8 私有发布（main.py --version-tag day0，Harbor + MS + HF 全私有）
                    步骤D9 报告收尾（generate_report.py + generate_day0_report.py）
```

**门控规则**（脚本层确定性判定，不信任 agent 自述）：
- `day0.smoke_ok=true` → 才进段2（与现有 v1_gate.py 同思路，新增 `day0_gate.py` 或内联 python）
- `day0.accuracy_ok=true` → 才执行性能
- `day0.eval_unreachable=true`（精度报错/无效且**无法解决**）→ 跳过性能，直接问题总结报告 + 结束
- 不可修复（`day0.unfixable=true`）→ 直接跳至问题总结报告，不发布
- 精度"有效但不达标且调优穷尽"→ 问题总结报告 + 跳过性能（按用户原始口径"精度无效时不需要性能评测"）

**长任务协议/断点续跑/段间越界检测/幂等检查**：从 run_pipeline.sh 同款机制移植（task_runner.py + state 轮询；每段前检查 ledger）。
**评测时长**：全量 gpqa_diamond 耗时长（thinking 模型可能 8h+），预算内预期，按长任务协议 detached 跑，`--max-timeout` 给足（如 86400s），禁止截断。

### 3.2 编排指令：`skills/flagos-day0/SKILL.md`（新增）

day0 专属编排指令，包含：
- 流程定义（D1-D9）、各步骤命令与 context 读写
- **排障决策树**（§4.1，指导框架；**修复环节授权 Claude 自由发挥**，决策树仅定顺序与边界）
- **组件源码修复授权与边界**（§4.2）
- 复用哪些既有 skill 的 tools（不复制脚本，只引用路径）
- CLAUDE.md 路由表新增一行：`day0 / 快速适配 / 冒烟测例 → flagos-day0`

### 3.3 状态模板：`shared/context_day0.template.yaml`（新增）

裁剪现有模板（去掉 V1/V2/V3/V4/plugin_workflow/optimization 等量产字段），新增 day0 专属段：

```yaml
day0:
  entry:
    image: ""            # 用户给定镜像（默认必含 plugin+flaggems+flagtree）
    weight_path_host: "" # 用户给定本地权重路径（宿主机）
    weight_path_container: ""  # 容器内挂载路径
  smoke:
    prompt: "中国首都在哪"
    passed: false
    answer: ""
    retries: 0           # 排障重试轮数
  repair:                # 排障动作记录（按序）
    - {round: 1, action: disable_ops, ops: [...], result: ""}
    - {round: 2, action: enforce_eager, result: ""}      # 仅启动阶段
    - {round: 3, action: source_patch, component: flaggems, files: [...], diff: "", result: ""}
  unfixable: false       # 不可修复 → 问题总结报告兜底终止
  unfixable_reason: ""
  accuracy_ok: false     # 相对 NV 退化 ≤5%
  eval_unreachable: false  # 精度报错/无效且无法解决（跳过性能）
  problem_summary:       # 问题总结报告数据源（修复过/不可修复/精度无效任一触发）
    phenomenon: ""
    actions_taken: ""
    root_cause: ""
    suggestion: ""
workflow_ledger:         # 独立步骤名 d1..d9，不与现有 ledger 混淆
  steps: [...]
```

部署：`setup_workspace.sh` 增加 `--context-template context_day0.template.yaml` 选项（默认行为不变）。

### 3.4 冒烟工具：`smoke_test.py`（抽离，新增）

从 `baseline_selector.py` 抽离 `SMOKE_PROMPT/SMOKE_KEYWORDS/smoke_test()` 为独立工具：
- 部署到容器 `/flagos-workspace/scripts/smoke_test.py`
- CLI：`python3 smoke_test.py --port <p> --model-name <n> [--prompt "中国首都在哪"] [--json]`
- 输出：`{"passed": bool, "answer": "..."}` + 退出码 0/1
- `baseline_selector.py` 改为 `from smoke_test import ...`（行为完全不变，纯抽离）
- 需求措辞"中国首都在哪"与现有"中国的首都是哪里？"语义等价，day0 默认采用需求措辞，关键词判定不变（北京/Beijing）

### 3.5 eager 模式：`start_service.sh --enforce-eager`（小改）

- start_service.sh 解析新增 `--enforce-eager`，透传 vLLM 启动参数 `--enforce-eager`
- **仅限启动阶段**（服务起不来时）在关算子无效后使用；冒烟阶段不使用（服务已起）
- 满足约束20：day0 SKILL.md 明确记录该参数的使用场景

### 3.6 报告：`shared/generate_day0_report.py`（新增）

独立脚本，不动 `generate_report.py`（避免影响现有报告口径）：
- 成功场景：复用 generate_report.py 生成常规报告（精度/性能/发布数据）
- **问题总结报告**（遇到问题时生成，核心兜底交付物）：
  ```
  # day0 问题总结报告：<模型名>
  ## 结论：❌ 无法适配（不可修复） / ⚠ 精度无效（已跳过性能） / ✅ 已修复（记录修复动作）
  ## 问题现象（启动日志/冒烟回答/精度分数/评测报错）
  ## 已尝试的排障动作（repair 记录：关算子列表、eager、源码 patch diff）
  ## 根因分析
  ## 建议修复方案（组件、文件、修改方向、验证方法）
  ## 附录：环境信息、日志摘要
  ```
- 修复后**不同步**上游仓库，修复 diff 仅进报告附录 + 随 `docker commit` 固化到私有镜像
- 复用 chip_spec.py 命名规范；输出 `results/[FAILED_]Nvidia_<模型>_day0_<ts>.md`

### 3.7 发布：`main.py --version-tag day0`（小改）

- `--version-tag` choices 增加 `"day0"`，镜像 tag 后缀 `-day0`（如 `<date_tag>-day0`）
- **不用 --only-harbor**：走标准发布阶段，Harbor + ModelScope + HuggingFace 三处，仓库**全部私有、不公开**（沿用现有"仓库可见性=全部私有"默认行为）
- 发布前 `docker commit` 容器 → 组件源码修复（若有）自动固化进镜像

---

## 4. 排障决策树（day0 核心差异化逻辑）

### 4.1 启动失败 / 冒烟测例失败

```
启动失败或冒烟不通过
 ├─ ① 优先按算子问题排查 → 日志分析 + diagnose_ops.py 定位 → 关算子
 │    ├─ 算子控制固定走 plugin env 路径：VLLM_FL_FLAGOS_BLACKLIST（复用约束26 + 记忆
 │    │  "plugin mode control_file noop"：plugin 环境控制文件空转，禁写控制文件）
 │    └─ 重启 → 冒烟重试（每轮可关多算子；累计禁用）
 ├─ ② 仍失败 → 探索 flagos 其它组件问题（plugin/flaggems/flagtree 组网不适配、注册缺失、
 │      dispatch 路径缺陷等）——此处授权 Claude Code 自由发挥：
 │    ├─ 判断复杂度：轻量可修（单函数/单文件 patch）→ 修 + 记录 diff 到 day0.repair
 │    └─ 重修复（跨模块/框架级）→ 不修，写建议方案
 ├─ (启动阶段专属) 关算子无效且疑似 graph capture/CUDA graph 问题 → --enforce-eager 重试
 └─ ③ 不可修复 → day0.unfixable=true → 问题总结报告 → 流程结束（不发布）
```

- **eager 仅限启动阶段**：服务已起（冒烟阶段）不再用 eager，直接走组件探索
- 冒烟失败先区分「服务崩溃」vs「服务存活但回答不含关键词」：
  - 崩溃 → 走 ① 算子排查循环
  - 存活但答错 → 记录回答原文，重试 1 次（低温/seed 抖动）；仍错 → 优先按关算子思路排查，无效则组件探索
- **修复自主权**：①的算子定位手段可自选（diagnose_ops.py、日志分析、traceback、kernel 名）；②的修复方式、改动位置由 Claude 判断，不设死板模板。边界仅两条：改完必须重启验证；diff 必须记录
- 修复产物：diff 记录进 `day0.repair` + 问题总结报告附录；**不同步** flagos-ai 上游仓库

### 4.2 精度不达标（进入段2后）

```
精度结果 vs NV 基线，rel_drop > 5%
 ├─ ① 优先定位问题算子（evalscope 保存的回复/结果分析 + operator_search.py 精度模式，
 │      plugin env blacklist）→ 关算子 → 重测（最多 3 轮，每轮记录算子与分数变化）
 ├─ ② 3 轮无效 → 整理问题/原因/修复方案 → 问题总结报告 → 不做性能 → 结束
 └─ ③ 评测报错/不可得 → 读 _last_error.json → 排查修复（算子/环境/脚本，Claude 自主）
      → 重试评测；仍无法解决 → eval_unreachable=true → 问题总结报告 → 跳过性能 → 结束
```

- 达标（rel_drop≤5%）→ `day0.accuracy_ok=true` → 正常进入性能评测
- **跳过性能的条件**（用户定稿口径）：精度**报错/无效且无法解决**；以及"有效但不达标且调优穷尽"
- 全量 gpqa_diamond 评测时间显著长于截断评测，调优每轮重测成本高——3 轮上限是预算保护，若第 3 轮仍呈收敛趋势可酌情 +1 轮（记录理由）

---

## 5. 文件清单

### 新增

| 文件 | 内容 | 规模 |
|------|------|------|
| `prompts/run_day0.sh` | day0 编排脚本（3 段 + 门控 + 报告收尾） | ~700 行 |
| `skills/flagos-day0/SKILL.md` | day0 编排指令 + 排障决策树 + 修复自主权边界 | ~300 行 |
| `shared/context_day0.template.yaml` | day0 状态模板 | ~150 行 |
| `skills/flagos-service-startup/tools/smoke_test.py` | 冒烟测例工具（抽离） | ~60 行 |
| `shared/generate_day0_report.py` | 问题总结报告生成器 | ~200 行 |
| `prompts/day0_gate.py`（或内联） | 段间门控判定（smoke_ok/eval 可达性） | ~80 行 |

### 修改

| 文件 | 改动 | 风险 |
|------|------|------|
| `skills/flagos-service-startup/tools/baseline_selector.py` | smoke_test 抽离为 import（行为不变） | 低 |
| `skills/flagos-service-startup/tools/start_service.sh` | 新增 `--enforce-eager` 参数透传 | 低 |
| `skills/flagos-container-preparation/tools/setup_workspace.sh` | 新增 `--context-template` 选项（默认不变） | 低 |
| `skills/flagos-release/tools/main.py` | `--version-tag` choices 增加 `day0` | 低 |
| `CLAUDE.md` | 路由表增加 day0 条目 + 场景说明 | 低 |

### 不动

`run_pipeline.sh`、现有 12 个 SKILL.md、`context.template.yaml`、`generate_report.py`、`nv_baseline.yaml`、所有调优/评测/发布工具核心逻辑。

---

## 6. 实施顺序（里程碑）

| 里程碑 | 内容 | 验证方式 |
|--------|------|---------|
| M1 工具底座 | smoke_test.py 抽离 + baseline_selector 适配 + start_service.sh eager + main.py day0 tag | 单测：smoke_test.py 对 mock 服务跑通；baseline_selector 回归不变 |
| M2 状态层 | context_day0.template.yaml + setup_workspace.sh 模板选项 + day0_gate.py | 手动造 context 跑 gate 判定矩阵 |
| M3 指令层 | skills/flagos-day0/SKILL.md + CLAUDE.md 路由 | 人工评审决策树覆盖度 |
| M4 编排层 | prompts/run_day0.sh（段构造/门控/长任务协议/断点续跑） | 用已知 OK 镜像+权重端到端 dry-run |
| M5 报告层 | generate_day0_report.py + 报告收尾集成 | 成功/问题两类场景各出一份报告 |
| M6 端到端 | 选 1 个已知可适配模型 + 1 个预期失败模型各跑一遍 | 全链路验收 |

---

## 7. 风险与开放问题（v2 修订后剩余项）

| # | 问题 | 建议 | 状态 |
|---|------|------|------|
| 1 | 全量 gpqa_diamond 评测时长长（thinking 模型可能 8h+），调优每轮重测成本高 | 长任务协议 + 时间预算内预期；3 轮调优上限 | 建议接受 |
| 2 | 发布 tag 用 `-day0` 需改 main.py choices（一行） | 采用 `-day0`（"怎么方便怎么来"下的最简做法） | 已定 |
| 3 | day0 是否需要批量模式（多模型串行） | 首版不做，编排脚本预留任务列表入口 | 建议后续 |
| 4 | 冒烟"服务存活但答错"重试后仍错，最终是否也归为不可修复→不发布 | 按 v2 口径：排障穷尽后 unfixable=true → 问题总结报告、不发布 | 默认如此 |

---

## 8. 兼容性承诺

- day0 分支所有改动不改变现有 1-15 步流程的任何行为（唯一交叉点 baseline_selector.py 为纯抽离）
- `refactor` 分支可安全合并 day0 的 M1/M2 工具底座改动（低风险项）
- day0 全流程产物（结果/报告/镜像）与现有流程产物通过独立 ledger 步骤名（d1-d9）和 `-day0` tag 区分，互不混淆
