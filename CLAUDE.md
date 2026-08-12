# FlagOS 自动化框架 — 项目级指令

> 此文件由 Claude Code 自动加载，提供 Skill 路由、工作流定义和自动决策规则。
> 各步骤的详细执行流程见对应 SKILL.md（通过路由表定位）。

---

## 自动初始化（每次会话启动时检查）

**在执行任何用户任务之前，先静默完成以下初始化**（不需要告知用户）：

权限配置 `settings.local.json` 已由 `run_pipeline.sh` 在启动前自动部署到 `.claude/settings.local.json`。**跳过此步骤，不要尝试创建或复制 settings 文件**。如果是交互式会话（非 pipeline 启动），可检查文件是否存在：

```bash
ls .claude/settings.local.json 2>/dev/null && echo "EXISTS" || echo "MISSING — 请手动执行: mkdir -p .claude && cp settings.local.json .claude/settings.local.json"
```

**注意**：`.claude/` 目录是 Claude Code 的敏感目录，headless 模式下写入会被拦截。pipeline 模式下此文件一定已存在，无需任何操作。

### context.yaml 使用规则（多任务隔离）

- `shared/context.template.yaml` 是模板文件，仅用于 `setup_workspace.sh` 初始化容器，**禁止直接读写**
- 运行时 context 位于容器内 `/flagos-workspace/shared/context.yaml`，每个容器独立，互不干扰
- 读取 context：`docker exec <container> cat /flagos-workspace/shared/context.yaml`
- 写入 context：通过 `docker exec <container>` 在容器内操作
- 宿主机快照：`/mnt/data/flagos-workspace/<model>/config/context_snapshot.yaml`（只读归档，由步骤8和兜底同步写入）
- 宿主机最终状态：`/mnt/data/flagos-workspace/<model>/config/context_final.yaml`（全流程结束时回传）

### 会话恢复检测

初始化完成后，检测是否存在未完成的流程。通过 `docker exec <container> cat /flagos-workspace/shared/context.yaml` 读取容器内 context，如果 `workflow.all_done != true` 且 `container.name` 非空：

1. 运行 `diagnose_failure.py --json` 获取诊断：
   ```bash
   docker exec <container> bash -c "PATH=${PY_BIN_DIR}:\$PATH python3 /flagos-workspace/scripts/diagnose_failure.py --json"
   ```
2. 输出诊断摘要给用户（中断位置、错误原因、恢复建议）
3. 根据诊断结果从中断点恢复（不从头重跑）

如果容器不存在或已停止，提示用户当前状态并询问是否重新开始。

---

## Skill 路由表

| 触发词 | Skill 名称 | SKILL.md 路径 |
|--------|-----------|---------------|
| 容器准备 / prepare container / 环境准备 | flagos-container-preparation | `skills/flagos-container-preparation/SKILL.md` |
| 环境检查 / inspect environment / 服务前检查 | flagos-pre-service-inspection | `skills/flagos-pre-service-inspection/SKILL.md` |
| 启动服务 / start service / 健康检查 | flagos-service-startup | `skills/flagos-service-startup/SKILL.md` |
| 性能测试 / benchmark / sglang bench | flagos-performance-testing | `skills/flagos-performance-testing/SKILL.md` |
| 算子替换 / operator replacement / 算子优化 | flagos-operator-replacement | `skills/flagos-operator-replacement/SKILL.md` |
| 精度评测 / eval correctness / accuracy test / 远端评测 / FlagRelease / flageval / 综合评测 / comprehensive eval / 本地评测 / quick 评测 / evalscope / GPQA | flagos-eval-comprehensive | `skills/flagos-eval-comprehensive/SKILL.md` |
| 日志分析 / analyze logs | flagos-log-analyzer | `skills/flagos-log-analyzer/SKILL.md` |
| 提交 issue / submit issue / report bug / 自动报告 | flagos-issue-reporter | `skills/flagos-issue-reporter/SKILL.md` |
| 组件安装 / install component / 安装 FlagGems / 安装 FlagTree / 升级 FlagGems / flag upgrade | flagos-component-install | `skills/flagos-component-install/SKILL.md` |
| 发布 / 镜像上传 / 镜像打包 / 模型发布 / release / publish / image upload / package image | flagos-release | `skills/flagos-release/SKILL.md` |
| 安装 plugin / install plugin / plugin 安装 / sglang-plugin | flagos-plugin-install | `skills/flagos-plugin-install/SKILL.md` |
| 离线推理 / offline inference / 模型适配 / inference 跑通 / 推理验证 | flagos-offline-inference | `skills/flagos-offline-inference/SKILL.md` |

---

## 工作流（新模型迁移发布）

**用户提供目标（容器名或镜像地址）+ 模型名后，四阶段 15 步全自动执行，零交互。**
**自动识别**：含 `:` 或 `/` 的目标视为镜像地址，否则通过 `docker inspect --type=container` 判断是否为已有容器。模型路径自动搜索，无需手动指定。

```
输入: 镜像 + 模型名  →  输出: 多版本镜像 + 报告

阶段一 确定基线 (V1)
1 容器准备           → 自动识别容器/镜像 + 模型权重搜索/下载 + 工具部署
2 环境检测           → inspect_env.py 场景分类 + FlagGems 集成分析
3 三选基线           → baseline_selector.py 三选状态机确定 V1 依赖:
                        V1.1 纯净基线(SGLANG_PLUGINS=__none__+USE_FLAGGEMS=0)
                        V1.3 插件层不开替换(SGLANG_PLUGINS=sglang_fl+USE_FLAGGEMS=0)
                        none 均失败→NV 兜底(精度回退 nv_baseline.yaml, 性能基线合成)
                      → 启动验证 + 冒烟 → 异常自动 issue
阶段二 使能 GT (V2)
4 精度评测           → V1/V2 GPQA Diamond 对比 → 异常自动 issue
5 精度调优           → [条件] env_type≠native 且 V2精度相对退化>5% 时分组排查定位问题算子（最多3轮）
6 性能评测           → V1/V2 4k1k benchmark 对比 → 异常自动 issue
7 性能调优           → [条件] env_type≠native 且 ratio<80% 时逐个禁用直到达标
8 产出GT镜像         → 打包 Harbor 私有(始终) + V2 精度达标才对外传权重/README，tag 后缀 -v2
阶段三 使能插件 (V3)   ← sglang 场景 V2 已走 sglang_fl 调度路径，此阶段=plugin 模式复测+交付
9 启动插件           → [plugin_entry=service_ok 触发] 以达标算子集 + plugin 模式启动
                        （install_plugin.py verify 确认可用，禁止重装）→ 崩溃则 issue + 停止
10 精度评测          → 与 V1 基线对比 → 不达标则算子调优（plugin 模式）
11 性能评测          → 与 V1 基线对比 → 不达标则算子调优（plugin 模式）
12 产出高覆盖度镜像   → tag 后缀 -v3，[不达标]issue + 镜像上传(私有) / [达标]镜像上传 + 更新 README
阶段四 性能优化 (V4)   ← 紧接 V3，V3 精度达标触发；性能不阻断，精度硬闸门
13 性能调优          → operator_reduction.py 阶段1 性能搜索（从 V3 算子池选 1~3 个只开，
                        仅当吞吐>当前基线才提交、基线动态推进，全程不测精度）
14 精度对齐          → operator_reduction.py 阶段2 精度回溯（按性能从高到低取组合测精度，
                        达标即产出，不达标回退次优，最坏回退 V3 等价；
                        保底≥1算子，精度相对退化≤5% 为成立前提）
15 产出高性能镜像     → tag 后缀 -v4，plugin 镜像模式发布
```

执行顺序：1 → 2 → 3 → 4 → [5] → 6 → [7] → 8 → [plugin_entry=service_ok] → 9 → 10 → 11 → 12 → [V3 精度达标] → 13 → 14 → 15
（**plugin/V3 入口 = service_ok**：V2 精度不阻塞 V3 尝试；**V4 入口 = service_ok AND V3(plugin)精度达标**，不叠加 V2 精度；性能全程不阻断，仅影响发布 tag 的 qualified 标签）
（对外发布(魔搭/HF)门控：V2 精度达标→步骤8建仓+传权重+README；V2 精度不达标→步骤8仅 Harbor 私有、不对外，若 V3 达标由步骤12 full-publish 补发；**V2 与 V3 都不达标→对外都不发**，仅留私有镜像）

**算子累计禁用规则**：5 禁用精度问题算子 → 6 在此基础上测性能 → 7 继续禁用性能问题算子。步骤 9-11 以步骤 5/7 的最终算子集为起点：**精度达标则不重新调优；精度不达标按精度三级递进（先 issue → plugin 模式关算子调优 → 全关仍不达标才判框架问题）在 plugin 模式下继续调优；性能不达标不强制重调，尽力即可、达上限即停**（见步骤 10/11 说明）。各步骤详细流程见对应 SKILL.md 的"编排层指令"章节。

**Plugin 流程特殊规则**：
- 触发条件（用户 2026-07-20 定稿，**入口只看能否起服务**）：步骤 8 完成且 `workflow.service_ok`（记为 **plugin_entry**，即 V2 环境能起服务）。**V2 精度不达标不阻塞 V3 尝试**——V2 与 V3 在 sglang 场景同走 sglang_fl 调度路径，但 V2 达标算子集与 V3 复测调优独立判定；V3 自己的精度在步骤10单独判。**性能不看重、不阻断**：performance_ok=false 不阻止步骤 9-12，性能仅决定发布 tag 的 qualified 标签。仅当 V2 服务起不来(service_ok=false)才 skip 步骤 9-12。
- 崩溃停止：步骤 9 启动插件（安装 verify + 服务启动）失败或崩溃 → 写 issue → 设 `plugin_workflow.crash_stopped=true` → **停止任务**
- Issue 路由：步骤 9-12 所有 issue 通过 `issue_reporter.py full --type plugin-error --repo flagos-ai/sglang-plugin-FL` 提交（只保存本地文件）
- **Plugin 阶段算子调优（精度三级递进）**：步骤 10 **精度**不达标时按三级递进处理：①先提交 issue 记录问题；②用 `operator_search.py run --plugin-mode --final-output-name v3_performance --state-path operator_config_v3.json` 在 Plugin 模式下继续关闭拖累精度的算子直到精度达标；③若全关 flaggems 算子仍精度不达标，判定为框架问题，提交 plugin-error issue，accuracy_ok=false（精度硬闸门未过 → V3 不产出）。步骤 11 **性能**不达标：仅写 performance-degraded issue + 标 performance_ok=false，**照常继续步骤12**（可选跑一次 plugin 性能调优尽力提升，达上限即停，不强求达标）。
- 算子集初始化：以主流程已达标的算子集（含步骤 5/7 的禁用列表）为起点，在此基础上累加禁用
- 镜像 tag：原 date_tag 追加 `-v3`（如 `202603301143-v3`）
- Plugin 不达标发布：调优后仍不达标时，先提交 issue，再打包镜像上传 Harbor（私有），不更新 ModelScope/HuggingFace README


### 版本定义

| 版本 | 定义 | 镜像 tag 后缀 | 产出方式 |
|------|------|--------------|---------|
| **V1** (基础版) | 原生 sglang，不加载 sglang_fl 插件、不开启 FlagGems（或关闭 flaggems 后无法起服务→无 V1） | `-v1` | 阶段一手动发布 |
| **V0** (中间态) | FlagGems 全量算子开启的初始状态 | — | 不发布（仅作为调优起点） |
| **V2** (Pro版) | FlagGems（sglang_fl plugin），精度相对退化≤5%，性能≥80% of V1 | `-v2` | 步骤8自动发布 |
| **V3** (Max版) | V2 + Plugin，精度相对退化≤5%，性能≥80% of V1 | `-v3` | 步骤12自动发布 |
| **V4** (精简版/Flag-express) | V3 基础上减算子以提升性能，追求性能绝对值最大化、**达标基准是超越 V3（不与 V1 比较）**，**精度相对退化≤5%（版本成立前提）**，保底≥1算子 | `-v4` | 步骤15自动发布（operator_reduction.py） |

- **V1 基线**：不开启 flaggems 算子替换的版本（不加载 sglang_fl 插件 + USE_FLAGGEMS=0 的原生 sglang），作为精度和性能基线。plugin 环境若关闭 flaggems 后无法启动服务（sglang_fl 与 sglang 深度耦合），则标记"无 V1"，跳过 V1 基线测试，精度基线回退 NV 基线（`nv_baseline.yaml`），**性能基线合成**：V2 使能 flaggems 后首次可正常启动时（步骤4之前、精度调优削减算子之前），quick 测一轮初始性能（`v2_initial_performance`），经 `synthesize_perf_baseline.py` ×1.05 合成基线（全芯片统一标准；吞吐×1.05、延迟÷1.05），按 `native_performance.json` 标准格式落盘（`_meta.synthetic=true` + `target_ratio_override=1.0` 标记），下游对比/调优/报告照常消费。合成基线场景达标线 = 基线×1.0 = **V2 初始的 1.05 倍**（`target_ratio_override` 覆盖默认 0.8，仅对合成基线生效，实测 V1 不受影响）
- **V0**：进入自动化时 flaggems 全量开启状态。服务启动后以运行时 [GEMS] 日志 / dispatch 记录的实际启用算子为准
- **V2 Pro**：经过算子调优（步骤5/7）后达标的版本。精度相对退化≤5%，性能≥80% of V1
- **V3 Max**：在 V2 基础上安装 Plugin 并调优达标的版本。允许 Plugin 模式下继续关闭算子
- **V4 精简 (Flag-express)**：在 V3 基础上通过 `operator_reduction.py` 减算子提性能，**两阶段**：阶段1 性能搜索（从 V3 基线起逐个试禁用，仅当禁用后吞吐 > 当前基线才提交、基线动态推进，全程不测精度）；阶段2 精度回溯（按性能从高到低取组合测精度，达标即产出，不达标回退次优，最坏回退 V3 等价）。**追求性能绝对值最大化，达标基准是超越 V3（不与 V1 比较，V1 仅报告参考）**，硬约束至少保留 1 个算子（plugin 也不例外）。**精度相对退化≤5% 是 V4 成立前提**，收尾做最终精度终检，不达标则 V4 不成立（success=False）。V4 成立需同时满足：超越 V3 + 保留≥1算子 + 精度达标
- **精度判据口径**：所有版本的精度达标均以「相对退化」计算——`rel_drop = (基线 - 当前) / 基线 ≤ 5%`，基线为本地 V1 或 NV 参考（`nv_baseline.yaml`）。见 `accuracy_compare.py`。**多数据集（`--datasets`）**：每个数据集独立判定（`accuracy_compare_{dataset}.json`），全部达标才 `accuracy_ok=true`；V3/V4 精度终检与 V4 基线取**主数据集**（第一个）

### 双 pipeline 分支（准入镜像分类驱动）

环境检测（步骤2 `inspect_env.py`）输出 `entry_image_type`，编排层（`run_pipeline.sh`）据此确定性路由到对应 pipeline，**无需 Claude 判断**：

| entry_image_type | 分支 | 准入镜像组成 | 版本路径 |
|------------------|------|--------------|---------|
| `gems_tree_plugin` | **B**（复杂） | flaggems + plugin（sglang_fl） | V1(三选 v1.1/v1.3/none) → V2(plugin 全量+调优) → V3(plugin 复测) → V4(减算子) |
| `native` | native | 无 flaggems | 仅精度/性能评测，不做算子调优与多版本发布 |

（sglang 分支无 `gems_tree` 代码注入态：flaggems 必须经 sglang_fl 插件生效，entry_image_type 收敛为 native | gems_tree_plugin）

**分支 B（gems_tree_plugin）工作流**：
- V1：**三选状态机**（`baseline_selector.py`，步骤3）确定基础版依赖——
  - `v1.1` SGLANG_PLUGINS=__none__ + USE_FLAGGEMS=0 纯净基线（原生 sglang，不加载插件不开替换）
  - `v1.3` SGLANG_PLUGINS=sglang_fl + USE_FLAGGEMS=0 插件层加载但不开算子替换（可测插件层固定开销）
  - 均失败 → `none`（强依赖 flaggems，sglang_fl 与 sglang 深度耦合），精度基线回退 `nv_baseline.yaml`
- V2：plugin 全量开启（USE_FLAGGEMS=1 + SGLANG_FL_PREFER=flagos），经步骤5/7 精度/性能调优达标
- V3：plugin 流程精度复测调优（步骤9-12），V2 与 V3 同走 sglang_fl 调度路径、同准入镜像（sglang 无代码注入，V2=V3 是分支常态）；段3 发布 V2 时即 `--also-tag v3` 双 tag（任何 V1 variant），V1=none 时额外跳过段4（V3 无独立确认必要），v1.1/v1.3 时段4 照常走步骤9-12 复测确认、步骤12 幂等复用已发布 v3 镜像收尾（用户 2026-08-12 定稿）
- V4：`operator_reduction.py`（步骤13-15）减算子优化性能

- **发布仓库**：V1/V2/V4 发布到 `harbor.baai.ac.cn/flagrelease-public`；**V3 发布到 `harbor.baai.ac.cn/flagrelease-project`**（交付 SVT 验收，V3=Max 为最终交付版）。**例外（sglang 分支常态双 tag）**：V2=V3 同镜像时 v3 由 `--also-tag` 继承 v2 路径推 public（同镜像同仓库双 tag），不单独切 project——除非 v3 未随段3 双 tag 发布（异常兜底走 --version-tag v3 时才切 project）

### native 场景工作流简化

纯原生环境无 FlagGems，工作流简化为：1容器准备 → 2环境检测 → 3服务启动 → 4精度评测 → 6性能测试 → 8发布。跳过所有 FlagGems 相关步骤。

### NV 重点场景

`sglang + sglang-plugin-FL + flaggems`（triton-cx 替代 flagtree）是当前 NV 模型发布的优先场景，推荐版本组合：`sglang 0.5.11 + sglang-plugin-FL + flaggems>=5.3.0rc2 + triton-cx 3.5.1`（勿装 flagtree——会卸载 triton-cx）。

---

## 环境场景定义

环境检测（步骤2）自动分类为以下场景之一，核心判定依据是 flaggems 是否存在：

| env_type | 判定条件 | FlagGems 控制 | 算子列表来源 |
|----------|---------|--------------|-------------|
| `native` | 无 flaggems（或有 flaggems 但无 sglang_fl 插件） | 无 | 无 |
| `sglang_plugin_flaggems` | 有 flaggems + sglang_fl 插件 | SGLANG_FL_* 环境变量（无代码注入/控制文件） | 服务日志 [GEMS] 行 / dispatch 日志 |

（sglang 分支无控制文件机制；补丁状态经 apply_patches.sh --verify 探测。各场景的 FlagGems 控制实现细节见 `skills/flagos-pre-service-inspection/SKILL.md` 和 `skills/flagos-service-startup/SKILL.md`。

---

## 自动决策规则（零交互默认值）

以下决策**直接执行，不询问用户**：

| 决策项 | 默认值 | 说明 |
|--------|--------|------|
| 目标识别 | 含 `:` 或 `/` → 镜像模式；否则 `docker inspect --type=container` 判断 | 避免镜像地址被误识别为同名容器 |
| 宿主机模型路径 | `check_model_local.py --no-download` 自动搜索。找到则使用实际路径挂载；未找到则使用 `/mnt/data/models/<model_name>` | `${MODEL_PATH}` 和 `${CONTAINER_MODEL_PATH}` 均取此路径 |
| docker run | 模板优先：严格按 SKILL.md 中 GPU 厂商对应模板执行。模板失败时先修正变量重试；仍失败则 `docker inspect` 借鉴已有容器重试一次；仍失败则终止 | 不需确认 |
| 精度评测 | 始终执行 V1 和 V2 | 不询问是否跳过 |
| 数据集参数 | `--datasets` 逗号分隔（`gpqa_diamond`/`mmlu`/`math_500`），默认 `gpqa_diamond`，作用于 run_pipeline.sh/run_batch.sh | 每个数据集独立评测、独立判定（per-dataset accuracy_compare_{dataset}.json），**全部达标才 accuracy_ok=true**（update_context.py 自动校验） |
| 评测时长预算 | thinking 模型（qwen3/qwq/deepseek-r1/r2/mimo/hunyuan 或 runtime.thinking_model=true）`--limit 30 --max-timeout 22500`；普通模型 `--limit 50 --max-timeout 7200`。V1/V2 参数必须相同。**数据集预算**：gpqa_diamond 显式传 `--limit`（30/50）；mmlu `--max-timeout 21600`（默认 100/子集=5700 题，不传 `--limit`）；math_500 `--max-timeout 7200`（默认全量 500 题，不传 `--limit`）；多数据集取各数据集 max_timeout 最大值，一律不传 `--limit` | 评测耗时长（thinking 6h+）是预算内预期，**禁止因耗时长跳过/放弃/截断评测** |
| 长任务执行协议 | 所有可能运行超过 10 分钟的命令（评测、服务等待、性能测试、算子调优、发布推送）一律走协议：写任务命令文件 → `task_runner.py --cmd ... --state ... --log ... --timeout <上限>` detached 启动（容器内 `docker exec -d`、宿主机 `python3 ... &`）→ Claude 每 8 分钟短轮询状态文件（`sleep 480 && cat <state> && tail -3 <log>`，单次调用 <10 分钟且必有输出）→ 会话被杀后任务继续跑、新会话读 state 断点接管 | **禁止** Bash(timeout=大数) 前台阻塞（Bash 工具 10 分钟硬上限，超过自动转后台 + 批次控制器 10 分钟无输出判会话失败），**禁止** TaskOutput 轮询；启动任务前先检查对应 state 文件，`status=running` 时直接接管禁止重复启动 |
| FlagGems 仓库地址 | `https://github.com/FlagOpen/FlagGems.git` | 无需用户提供 |
| 性能目标 | quick: 4k_input_1k_output 并发 64 ratio ≥ 80%；comprehensive: 每个用例每个并发级别均 ≥ 80%。**判定粒度：每个数据点的 min ratio** | 不询问 |
| V1 性能基线缺失 | 步骤4前 V2 初始性能 quick 一轮 → `synthesize_perf_baseline.py` ×1.05 合成 `native_performance.json`（全芯片统一，`_meta.synthetic=true`，达标线=V2初始×1.05） | 报告按 native 基线展示 |
| pip install 模式 | `pip install .`（非 editable） | 避免 `-e .` 在容器中的问题 |
| pip 国内镜像 | `-i https://mirrors.aliyun.com/pypi/simple/` | pip 失败时自动加镜像重试 |
| 服务端口 | 默认 8000，被占用则自动递增（+1 到 +10） | 不询问端口号 |
| GPU 设备 | 启动前检测空闲 GPU（显存占用 <5%），仅使用空闲 GPU | 不询问使用哪些卡 |
| Harbor 仓库地址 | `harbor.baai.ac.cn/flagrelease-public` | 无需用户提供 |
| 模型仓库命名 | `FlagRelease/{Model}-{vendor}-FlagOS` | 自动生成 |
| 仓库可见性 | 全部私有发布 | 报告中注明是否达标 |
| 容器内模型搜索路径 | `/data,/models,/root,/home,/workspace,/mnt,/opt` | 不询问 |
| 容器内模型下载目录 | 镜像模式：下载到已挂载的 `${CONTAINER_MODEL_PATH}`；容器模式：优先已挂载宿主机卷路径 | 镜像模式下模型权重保证落在宿主机 |
| 镜像模式容器名冲突 | 追加时间戳后缀 `_MMDD_HHMM` 创建新容器 | 禁止复用已有容器 |
| 精度调优触发 | `accuracy_ok=false` 且 `env_type≠native` 时自动触发 | 不询问 |
| 性能调优触发 | `performance_ok=false` 且 `env_type≠native` 时自动触发 | 不询问 |
| V3 验证 benchmark | quick 模式 | 不询问策略 |
| Plugin 流程触发 | `workflow.qualified=true` 后自动进入步骤 9-12 | 不询问是否安装 plugin |
| Plugin 安装失败 | 写 issue 到 `flagos-ai/sglang-plugin-FL` → 停止任务 | 不尝试恢复 |
| Plugin 服务崩溃 | 写 issue 到 `flagos-ai/sglang-plugin-FL` → 停止任务 | 不切回非 plugin 模式 |
| Plugin issue 路由 | 步骤 9-12 所有 issue → `flagos-ai/sglang-plugin-FL` | 非 FlagGems 仓库 |
| Plugin 镜像命名 | 原 tag 追加 `-plugin` 后缀 | 自动生成 |
| Plugin 算子集 | 复用主流程已达标的算子集（含步骤 5/7 禁用列表） | 达标不重新调优；不达标进三级递进继续调优 |
| 网络代理切换 | 从 `FLAGOS_PROXY_LIST` 逐个尝试 | 网络操作失败时自动切换代理重试，全部失败才终止 |
| 容器内代理传递 | `docker exec -e http_proxy=<proxy> -e https_proxy=<proxy>` | 所有需要外网的 docker exec 命令必须传入代理 |

---

## 用户交互规则

**1-13 全自动执行，零交互。** 网络失败自动切换代理重试，全部代理失败则终止任务，不询问用户。

凭证均通过环境变量提供：`HARBOR_USER`/`HARBOR_PASSWORD`、`MODELSCOPE_TOKEN`、`HF_TOKEN`、`GITHUB_TOKEN`。
代理通过 `--proxy` 参数或环境变量 `http_proxy`/`https_proxy` 提供。

---

## 工具脚本部署

容器准备阶段（步骤1完成后），通过 `setup_workspace.sh` 一次性部署所有工具：

```bash
bash skills/flagos-container-preparation/tools/setup_workspace.sh $CONTAINER
```

部署的脚本清单：`inspect_env.py`、`toggle_flaggems.py`、`wait_for_service.sh`、`benchmark_runner.py`、`performance_compare.py`、`operator_optimizer.py`、`operator_search.py`、`diagnose_ops.py`、`eval_monitor.py`、`install_component.py`、`install_flagtree.sh`、`issue_reporter.py`、`log_analyzer.py`、`install_plugin.py`、`apply_patches.sh`（含 `patches/` 目录）。

---

## 宿主机工作目录结构

```
/mnt/data/flagos-workspace/<model>/          ← 挂载到容器 /flagos-workspace
├── results/                              # 最终交付物
├── traces/                               # 每步留痕（JSON）
├── logs/                                 # 运行日志
└── config/                               # 使用的配置快照
```

目录创建时机：容器准备阶段由 `setup_workspace.sh` 自动创建。

### 历史数据归档

`setup_workspace.sh` 在每次流程启动时自动检测上一轮产出数据。若 `results/`、`traces/`、`logs/` 任一非空，自动将其移入 `archive/<YYYYMMDD_HHMMSS>/`。

---

## Trace 留痕规范

**强制规则**：每个 Skill 完成后，必须在 `traces/` 下写入对应步骤的 trace JSON 文件。

**计时强制规则**：每个 Skill 开始时记录 `timestamp_start`，结束时记录 `timestamp_end` 和 `duration_seconds`。完成 trace 写入后同步更新 `context.yaml` 的 `timing.steps.<step_name>` 字段。

### Trace JSON 统一格式

```json
{
  "step": "01_container_preparation",
  "title": "容器准备",
  "timestamp_start": "ISO 8601",
  "timestamp_end": "ISO 8601",
  "duration_seconds": 120,
  "status": "success | failed | skipped",
  "actions": [{"action": "...", "command": "...", "timestamp": "...", "status": "...", "output_summary": "..."}],
  "result_files": ["results/..."],
  "context_updates": {"field": "value"},
  "_meta": {"字段名": "说明"}
}
```

写入方式：通过 `docker exec $CONTAINER bash -c "cat > /flagos-workspace/traces/XX.json << 'TRACE_EOF' ... TRACE_EOF"`

---

## 工作流台账维护规范

**强制规则**：编排层在每个 Skill 开始和结束时，必须实时更新 `context.yaml` 的 `workflow_ledger.steps[]` 对应条目。

状态流转：`pending → in_progress → success | failed | skipped`

| 时机 | 更新字段 |
|------|---------|
| Skill 开始 | `status: "in_progress"`, `started_at` |
| Skill 成功 | `status: "success"`, `finished_at`, `duration_seconds`, `notes` |
| Skill 失败 | `status: "failed"`, `finished_at`, `duration_seconds`, `fail_reason` |
| Skill 跳过 | `status: "skipped"`, `skip_reason` |

四者互补：台账、trace、timing、report。遇到步骤完成时四个都要更新。

**强制规则**：每个 Skill 完成后，必须调用 `generate_report.py` 更新报告（覆盖写入，脚本自动备份上一版）：

```bash
docker exec $CONTAINER bash -c "PATH=${PY_BIN_DIR}:\$PATH python3 /flagos-workspace/scripts/generate_report.py --output /flagos-workspace/results/report.md"
docker exec $CONTAINER bash -c "PATH=${PY_BIN_DIR}:\$PATH python3 /flagos-workspace/scripts/generate_report.py --summary"
```

---

## 流水线执行日志规范

`logs/pipeline.log` 由 `prompts/stream_filter.py --pipeline-log` 在 Claude 进程外部自动生成。

**对 Claude 输出的格式约定**（确保 stream_filter.py 能正确提取）：
- 步骤开始：`[步骤1] 容器准备 — 开始`
- 步骤完成：`[步骤1] 容器准备 — 完成 (1m 9s)`
- 步骤失败：`[步骤3] 启服务 — 失败`
- 步骤跳过：`[步骤4] 精度评测 — 跳过`
- 关键结果：`✓ env_type=sglang_plugin_flaggems, flaggems=5.3.0rc2`
- 异常事件：`✗ V2/V1 性能比 72.1% < 80%`

详细格式示例见 `skills/shared/pipeline_log_spec.md`。

---

## 问题日志规范

遇到服务启动异常、精度异常、性能不达标时，必须追加写入对应的 issue log 文件：

| 文件 | 写入时机 |
|------|---------|
| `logs/issues_startup.log` | 服务启动失败、崩溃（不含超时） |
| `logs/issues_accuracy.log` | V2精度相对退化 >5%、评测报错 |
| `logs/issues_performance.log` | 任一并发级别 V2/V1 < 80% |

统一格式：
```
[YYYY-MM-DD HH:MM:SS] <版本(V1/V2)> | <问题摘要>
  详情: <错误信息/数值>
  操作: <采取的措施>
  结果: <措施结果>
```

追加写入（`>>`），与 trace 互补，遇到问题时两个都要写。

---

## 网络问题处理策略

### 代理切换机制

流程启动时通过 `--proxy` 参数传入代理列表（逗号分隔），自动检测选出最佳代理。运行中网络操作失败时，从 `FLAGOS_PROXY_LIST` 逐个切换代理重试，全部失败才终止。

- 宿主机代理：通过环境变量 `http_proxy`/`https_proxy` 生效（docker push、modelscope/hf 上传）
- 容器内代理：通过 `docker exec -e http_proxy=<proxy> -e https_proxy=<proxy>` 传入
- 容器内代理列表文件：`/flagos-workspace/.proxy`（每行一个代理地址）
- 所有需要外网的 `docker exec` 命令必须传入代理环境变量

### pip install 失败

按以下顺序自动尝试镜像源，**不询问用户**：
1. 阿里云：`-i https://mirrors.aliyun.com/pypi/simple/`
2. 清华：`-i https://pypi.tuna.tsinghua.edu.cn/simple/`
3. 腾讯：`-i https://mirrors.cloud.tencent.com/pypi/simple/`
4. 全部失败 → 记录错误，终止任务

### 其他网络操作失败

第一次失败且错误包含网络关键词 → 切换代理重试。所有代理均失败 → 终止任务。

---

## 关键约束

### 工具使用约束

1. **性能测试只能通过 `benchmark_runner.py` 执行**，禁止直接运行 `sglang bench_serving`
2. **FlagGems 开关只能通过 `toggle_flaggems.py` 切换**，禁止手动 sed
3. **FlagGems 安装只能通过 `install_component.py` 执行**，禁止手动 pip install；**FlagTree 在 sglang 场景禁止安装**（会卸载 triton-cx，install_component.py 已内置硬闸门返回 skipped）
4. **Issue 生成只能通过 `issue_reporter.py` 执行**，禁止手动拼 `gh issue create`。Issue 只保存为本地 markdown 文件，代码层面已禁用 GitHub API 自动提交
5. **性能对比必须通过 `performance_compare.py` 执行**，禁止手动计算 ratio
6. **步骤7性能算子调优必须通过 `operator_search.py run` 一次性执行**。禁止编排层手动拼凑循环。`operator_search.py` 失败时禁止手动重试，应直接标记失败并继续流程
7. **工具脚本必须从项目目录或容器内 `/flagos-workspace` 执行**，禁止复制到 `/tmp`

### 环境约束

8. **所有操作在 `/flagos-workspace` 目录下执行**，产出文件按类型分目录
9. **容器内 `/flagos-workspace/shared/context.yaml` 是 Skill 间共享状态**，每个 Skill 完成后必须通过 `docker exec` 更新（禁止操作 `shared/context.template.yaml`）
10. **容器内 Python 必须用 sglang 环境 python**（/usr/local/python3.11.14/bin，可用 PYTHON_BIN_DIR 覆盖）。所有 `docker exec` 中的 python3/pip 命令必须加 `PATH=${PY_BIN_DIR}:$PATH` 前缀（PY_BIN_DIR 默认 /usr/local/python3.11.14/bin）
11. **宿主机 mkdir/ls 严禁使用花括号展开**。`mkdir -p /path/{a,b,c}` 会被 sandbox 拦截。容器内不受此限制
12. **Claude Code Bash 工具受沙箱限制**。外部路径文件读写必须通过 `docker exec` 或 `docker cp`。禁止直接操作 `/mnt/data/...` 等宿主机路径
13. **中间文件禁止写入项目源码目录**。只能写入 `/mnt/data/flagos-workspace/<model>/config/` 或容器内 `/flagos-workspace/config/`

### GPU 资源管理

14. **V1 和 V2 必须使用相同的 GPU 配置**，禁止重新检测 GPU
15. **V1/V2 模式切换前必须先停止当前服务释放 GPU**。必须先 `docker restart $CONTAINER && sleep 5`
16. **每个 segment 结束时必须停止推理服务释放 GPU 显存**
17. **每轮算子搜索前必须验证 GPU 显存已释放**。`operator_search.py` 自动处理，连续清理仍无可用 GPU 则中止

### 流程约束

18. **流程不可中途终止**。精度/性能不达标不是终止理由，标记 `ok=false` 继续下一步，最终走到步骤8（私有发布）。唯一允许终止：Claude API 本身不可用。**例外**：步骤3 FlagGems 崩溃且算子诊断重试连续 2 轮确认无任何可归因算子的新错误（工具 + 人工日志分析均无结果），设 `workflow.service_ok=false` → 提交 issue 后跳过步骤4-7，直接到步骤8（私有发布）。崩溃诊断不限轮次——每轮能定位到新问题算子就继续禁用并重试。`diagnose_ops.py` 返回空不等于"无新算子"，必须自行从日志中分析
18a. **启动崩溃第一原则：禁用算子是最高优先解**。步骤3 遇到任何形式的崩溃（AICore 异常、Triton 编译错误、graph capture 失败、RuntimeError 等），必须首先定位并禁用具体算子。在穷尽所有算子定位手段（diagnose_ops.py + 人工日志分析 + traceback 中 flag_gems 路径 + 崩溃前编译的 kernel 名）之前，**严禁**尝试 enforce-eager、切 native、或判定不可恢复。enforce-eager 仅作为"已禁用所有可疑算子后仍崩溃"时的最后辅助手段，不是替代算子排查的捷径
19. **精度评测和性能测试严禁同时进行**，必须等一个完全结束后再启动另一个。整体串行：4 → 5 → 6 → 7。**唯一例外**：无 V1 场景（`baseline.v1_available=false` 或 V1 无法启动）在步骤4之前插入一次 V2 初始性能测量（quick 一轮）用于合成基线——仍是串行执行（先性能后精度），不违反互斥本意
20. **禁止添加 SKILL.md 未记录的 vLLM 启动参数**，遇到启动问题应分析日志找根因
21. **V1 和 V2 精度评测必须使用完全相同的参数**。包括 max_tokens、题目数量、评测脚本版本，禁止任何一方使用不同配置
22. **性能测试 output-name 标准命名**：V1=`native_performance`，V2=`flagos_performance`，V3=`flagos_optimized`
23. **步骤5/7的 trace 文件独立**，不混入步骤4/6的 trace
24. **步骤7性能算子调优最多 2 轮**（`operator_search.py run --max-rounds 2`），elimination 策略，每轮 benchmark 使用 quick 模式，达标即停；2 轮内未达标**不阻塞流程**，标 `performance_ok=false` 尊重实测结果继续下一步（性能不达标不是终止理由，见约束18）。步骤5精度调优最多 3 轮（见 flagos-eval-comprehensive SKILL.md）
25. **每次服务启动前必须清理 Triton/FlagGems 编译缓存**。`start_service.sh` 已内置此逻辑。手动启动时也必须执行 `rm -rf /root/.triton/cache/ /tmp/triton_cache/ /root/.flaggems/code_cache/`。确保每次启动在干净状态下暴露所有问题算子，禁止依赖旧缓存侥幸通过

### 算子控制约束

26. **禁用算子逐步累计，全流程传递**。步骤3崩溃诊断 → 步骤5精度调优 → 步骤7性能调优 → 步骤9-11 Plugin，每步在前序基础上累加禁用。算子控制方式按场景区分：
    - **sglang 场景（`sglang_plugin_flaggems`，含步骤 9-11）**：无代码注入控制文件机制（`FLAGGEMS_CONTROL_MODE` / `/root/flaggems_ops_control.json` 均不适用），算子配置统一走 SGLANG_FL_* env。步骤 9 启动插件前通过 `persist_op_config.py --auto --env-type sglang_plugin_flaggems` 将算子配置固化到容器 `/etc/environment`（`USE_FLAGGEMS`、`SGLANG_FL_PREFER`、`SGLANG_FL_FLAGOS_WHITELIST`/`BLACKLIST`、`SGLANG_PLUGINS`）。步骤 9-11 启动服务优先使用 `start_service.sh --mode flagos`（自动加载固化变量），无需编排层手动传递内联环境变量。兜底时仍可通过 `apply_op_config.py --mode custom --flagos-blacklist "op1,op2,..."` 生成 `env_inline` 内联传递
    - 两种场景均禁止使用 `toggle_flaggems.py --action enable`（会重置为全量开启）
27. **算子列表以运行时 txt（`flaggems_enable_oplist.txt` 或 `gems.txt`）为唯一权威来源**。每次服务启动后必须检查该文件。不在此文件中的算子必须被显式关闭。算子调优中的关闭列表只是控制输入，实际生效以运行时 txt 为准

### 数据完整性约束

28. **每个 Skill 完成后必须写入对应的 trace JSON**
29. **workflow 状态字段必须与实际数据一致**。`accuracy_ok=true` 仅当V2精度相对退化 ≤ 阈值；`performance_ok=true` 仅当 min_ratio ≥ target_ratio。**禁止直接通过 `update_context.py --set workflow.performance_ok=true` 或 `--set workflow.accuracy_ok=true` 设置**——这两个字段只能由 `operator_search.py`（调优达标时自动设置）或 `update_context.py` 的内置校验逻辑设置。`update_context.py` 已增加写入校验：设置这两个字段为 true 时会自动验证最新结果文件，不达标则拒绝写入
30. **工具脚本失败后必须读取 `/flagos-workspace/logs/_last_error.json`**，将错误同步到 context.yaml
31. **流程中断后自动诊断**。新会话启动时应优先读取 `logs/failure_diagnosis.json` 了解中断原因
32. **编排层生成的 JSON 必须包含 `_meta` 字段说明**

---

## 权限预配置

`settings.local.json` 已预配置 docker/pip/curl/nvidia-smi 等常用命令白名单，由 `run_pipeline.sh` 自动部署到 `.claude/settings.local.json`。详见文件内容。
