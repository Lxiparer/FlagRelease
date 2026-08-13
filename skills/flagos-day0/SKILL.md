# FlagOS day0 快速适配验证 Skill

> 场景：给定**镜像（必含 plugin+flaggems+flagtree 全组件）+ 本地权重路径**，
> 完成全组件服务启动 → 首都冒烟测例（硬闸门）→ 全量 gpqa_diamond 精度评测 →
> 精度调优 → 性能采集 → 私有发布（Harbor + ModelScope + HuggingFace 全私有）。
> 由 `prompts/run_day0.sh` 分段编排（段1: D1-D4 / 段2: D5-D6 / 段3: D7-D9），
> 本文档是每个段内 Claude 的执行指令。

---

## 强制约束（day0 专属，优先级高于通用 CLAUDE.md 约束）

1. **冒烟硬闸门**：首都测例通过才进精度评测；段间 gate 由编排层 `day0_gate.py` 判定，不信任自述。
2. **修复自主权**：排障时允许自由选择定位手段与修复方式（决策树只定顺序与边界）。边界仅两条：
   - 改完必须**重启服务 + 冒烟重测**验证；
   - 所有排障动作（关算子/改文件 diff/环境变更）必须记录进 `day0.repair`。
3. **eager 仅限启动阶段**：`--enforce-eager` 只在服务起不来时、关算子无效后使用；服务已起后禁用。
4. **算子控制固定走 plugin env 路径**：`VLLM_FL_FLAGOS_BLACKLIST`（经 `apply_op_config.py` 生成
   `env_inline` 启动前缀）。**禁止写控制文件**（plugin 下 `VLLM_FL_PREFER_ENABLED=true` 会使控制文件空转）。
5. **精度无效不做性能**：评测报错/无效且无法解决，或调优穷尽仍不达标 → 问题总结报告 → 跳过性能 → 结束。
6. **修复不同步上游**：组件源码修复只进报告附录 + 随 `docker commit` 固化进私有镜像，不提交 flagos-ai。
7. **发布全私有**：`main.py --version-tag day0` 走标准发布（Harbor + MS + HF 三处私有仓），**不用 --only-harbor**。
8. 本流程**没有 V1 基线**：精度基线 = `nv_baseline.yaml`（NV 参考分）；性能无对比无闸门，仅采集。
9. 本流程**没有 V2/V3/V4 版本体系**：单一交付，tag `-day0`。

---

## 上下文与工具位置

- 运行时状态：容器内 `/flagos-workspace/shared/context.yaml`（day0 模板，字段见 `shared/context_day0.template.yaml`）
- 写 context：`docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/update_context.py --set key.path=value --json"`
- 工作目录：容器内 `/flagos-workspace`；产出分类落 `results/ traces/ logs/ config/`
- 变量约定：`CONTAINER`=容器名，`MODEL`=模型名（权重路径 basename），`PORT`=服务端口
  （实际端口读容器内 `/flagos-workspace/logs/service_port`，端口会因占用自动递增）

## 流程总览（段边界严格遵守）

```
段1（D1-D4 准备+启动+冒烟） → day0_gate smoke → 段2（D5-D6 精度+调优）
  → day0_gate accuracy → 段3（D7-D9 性能+发布+报告）
任一不可修复/精度无效 → 跳过后续段，直接问题总结报告收尾
```

---

## 步骤 D1 — 容器准备

1. 用给定镜像创建容器（GPU 厂商对应 docker run 模板见 flagos-container-preparation/SKILL.md，
   挂载权重路径 `${DAY0_WEIGHT_PATH_HOST}` → 容器内 `${DAY0_WEIGHT_PATH_CONTAINER}`；
   容器名 `<模型名>_day0`，重名追加 `_MMDD_HHMM`）
2. 部署工具（day0 模板初始化）：
   ```bash
   bash skills/flagos-container-preparation/tools/setup_workspace.sh ${CONTAINER} ${MODEL} \
     --context-template=context_day0.template.yaml
   ```
3. 写 context：`entry.image/entry.weight_path_*`、`model.name`（basename）、`model.container_path`、
   `container.name/status`、`image.name/tag`、`workspace.host_path`
4. trace `traces/d1_container_preparation.json`；ledger `d1_container_preparation`

## 步骤 D2 — 环境检测（仅采集信息，不参与分支路由）

```bash
docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/inspect_env.py --json"
```

- 确认三组件齐全：`inspection.has_plugin=true`、`has_flagtree=true`、flaggems 版本非空。
  **day0 镜像默认必为全组件**；若缺失即镜像不符合 day0 场景 → 记录并走问题总结报告（unfixable）
- 采集 GPU 厂商/型号/数量、空闲 GPU 检测、TP 推算（calc_tp_size.py）、`runtime.thinking_model`
  （评测预算：thinking 模型全量 gpqa_diamond 耗时 8h+ 属预算内预期，禁止截断/放弃）
- trace `traces/d2_environment_inspection.json`；ledger `d2_environment_inspection`

## 步骤 D3 — 全组件服务启动

```bash
# 长任务协议三步（见文末）执行：
docker exec ${CONTAINER} bash -c "cd /flagos-workspace/scripts && bash start_service.sh --mode flagos"
docker exec ${CONTAINER} bash -c "cd /flagos-workspace/scripts && bash wait_for_service.sh --port ${PORT} --timeout 180 --max-timeout 5760 --mode flagos --model-name '${MODEL}' --log-path /flagos-workspace/logs/startup_flagos.log"
```

- 全组件 = plugin（VLLM_PLUGINS=fl，start_service.sh 自动探测）+ FlagGems 全量 + FlagTree
- 启动后**强制检查运行时算子列表**：`/tmp/flaggems_enable_oplist.txt`（唯一权威来源），
  存在且非空 = FlagGems 实际生效；记录 `service.enable_oplist_path/count` 与 `initial_operator_list`
- **启动失败 → 进入排障决策树**（见下），不自作主张跳过
- trace `traces/d3_service_startup.json`；ledger `d3_service_startup`

## 步骤 D4 — 首都冒烟测例（硬闸门）

```bash
docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/smoke_test.py \
  --port ${PORT} --model-name '${MODEL}' --prompt '中国首都在哪' --json"
```

- 判定：回答含关键词（北京/Beijing）。通过 → `day0.smoke.passed=true` + 记录 answer → 段1 完成
- 不通过 → 先区分：**服务崩溃**（进程死了/接口 5xx）vs **存活但答错**：
  - 崩溃 → 排障决策树（算子优先）
  - 答错 → 记录回答原文，重试 1 次（低温抖动）；仍错 → 排障决策树（优先关算子思路，无效则组件探索）
- trace `traces/d4_smoke_test.json`；ledger `d4_smoke_test`

### 排障决策树（启动失败 / 冒烟失败）

```
① 优先算子问题排查：日志分析 + diagnose_ops.py + traceback 中 flag_gems 路径 + 崩溃前编译的 kernel 名
   → 定位到算子 → 关掉重试（累计禁用）
   关闭方式：apply_op_config.py --mode custom --flagos-blacklist 'op1,op2' 生成 env_inline 启动前缀
   （plugin 场景黑名单按函数名精确匹配——注意变体：禁 addmm 需同时考虑 addmm_out 等，可自行展开）
② 关算子无效且处于启动阶段 → start_service.sh --enforce-eager 重试（day0.enforce_eager_used=true）
③ 仍失败 → 探索 flagos 其它组件问题（plugin/flaggems/flagtree 组网不适配、注册缺失、dispatch 缺陷）：
   判断复杂度 → 轻量可修（单文件 ≤~50 行、影响面限单组件）就修，记录 diff 进 day0.repair
             → 重修复（跨模块/框架级）不修，写建议方案
④ 不可修复 → day0.unfixable=true + 原因 → 问题总结报告 → 流程结束（不发布）
```

每轮排障：重启 → wait_for_service → 冒烟重测；动作与结果 append 到 `day0.repair`（round/action/ops|files/result/note）。
排障不限轮次，但每轮必须能归因（关了什么/改了什么/结果如何）。

---

## 步骤 D5 — 精度评测（全量 gpqa_diamond，基线 = NV）

长任务协议三步（`<TASK_ID>=day0_eval`），cmd 文件内容：

```bash
cd /flagos-workspace/scripts
python3 eval_wrapper.py --eval-cmd 'python3 fast_gpqa.py --config fast_gpqa_config.yaml --dataset gpqa_diamond --limit 0 --output /flagos-workspace/results/day0_gpqa_result.json' \
  --service-log /flagos-workspace/logs/startup_flagos.log \
  --stall-timeout 300 --max-timeout 86400
```

- **必须传 `--limit 0`**（全量 198 题；不传时 fast_gpqa 默认只跑 50 题——2026-08-13 实测确认）；task_runner `--timeout 86400`
- 评测期间服务崩溃 → eval_wrapper 报 service_crash → 重启服务后重试评测
- 完成后对比判定（NV 基线模式）：
  ```bash
  docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/accuracy_compare.py \
    --v2 /flagos-workspace/results/day0_gpqa_result.json \
    --nv-baseline '${MODEL}' --metric gpqa_diamond \
    --output /flagos-workspace/results/accuracy_compare_day0.json --json"
  ```
- 写 context：`eval.score/nv_score/rel_drop_pct`；`day0.accuracy_ok=true` 当 rel_drop≤5%
- **NV 基线缺失**（模型不在 nv_baseline.yaml）→ 精度无法判定 → 问题总结报告（建议补基线），
  `day0.eval_unreachable=true`，跳过性能
- trace `traces/d5_accuracy_eval.json`；ledger `d5_accuracy_eval`

## 步骤 D6 — 精度调优（不达标时，≤3 轮）

```
rel_drop > 5%
 ├─ ① 定位问题算子：evalscope 留存的逐题回复/结果分析 + 错误题与算子相关性日志 → 关算子
 │     apply_op_config.py --flagos-blacklist（累计）→ 重启 → 冒烟确认服务未坏 → 重测精度
 │     最多 3 轮；每轮记录关闭算子与分数变化到 day0.repair + eval.excluded_ops_accuracy
 ├─ ② 3 轮无效 → 判断是否非算子因素（数据/组网/框架特性）→ 整理问题/原因/修复方案
 │     → day0.eval_unreachable=true → 问题总结报告 → 跳过性能 → 结束
 └─ ③ 评测报错/不可得 → 读 logs/_last_error.json 排查（算子/环境/脚本，自主发挥）→ 重试评测 1 次
       → 仍失败 → day0.eval_unreachable=true + 原因 → 问题总结报告 → 跳过性能 → 结束
```

- 达标即停（`day0.accuracy_ok=true`）
- 全量 gpqa_diamond 每轮重测成本高（hours 级），3 轮是预算保护；第 3 轮呈收敛趋势可 +1 轮并记录理由
- trace `traces/d6_accuracy_tuning.json`；ledger `d6_accuracy_tuning`

---

## 步骤 D7 — 性能评测（单轮采集，无对比无闸门）

```bash
# 长任务协议（<TASK_ID>=day0_bench）
cd /flagos-workspace/scripts
python3 benchmark_runner.py --output-name day0_performance --quick --port ${PORT} --model '${MODEL}'
```

（参数以 benchmark_runner.py --help 为准；quick=4k_input_1k_output 并发 64）
- 结果写 `perf.output_throughput/total_throughput/result_path`；**不做 ratio 计算、不做调优**
- trace `traces/d7_performance.json`；ledger `d7_performance`

## 步骤 D8 — 私有发布（Harbor + MS + HF 全私有，tag -day0）

1. 同步 context 到宿主机：`docker cp ${CONTAINER}:/flagos-workspace/shared/context.yaml /data/flagos-workspace/${MODEL}/config/context_snapshot.yaml`
2. 宿主机长任务协议执行（可能数小时，镜像推送）：
   ```bash
   python3 skills/flagos-release/tools/main.py \
     --from-context /data/flagos-workspace/${MODEL}/config/context_snapshot.yaml \
     --version-tag day0
   ```
   - 环境变量：`MODELSCOPE_TOKEN` / `HF_TOKEN` / `HARBOR_USER` / `HARBOR_PASSWORD`
   - 标准 publish 阶段：commit → Harbor push → MS/HF 建仓（**私有**）+ 传权重 + README
   - **不用 --only-harbor**（day0 私有发布 = 三处全私有不公开）
3. 完成回传：`docker cp ${CONTAINER}:/flagos-workspace/shared/context.yaml /data/flagos-workspace/${MODEL}/config/context_final.yaml`
4. 写 `release.harbor_image/modelscope_url/huggingface_url`、`workflow.released=true`
5. trace `traces/d8_release.json`；ledger `d8_release`

## 步骤 D9 — 报告收尾

```bash
docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/generate_day0_report.py \
  --context-yaml /flagos-workspace/shared/context.yaml --output-dir /flagos-workspace/results/ --json"
```

- 成功场景：常规报告（精度/性能/发布信息，命名 `Nvidia_<模型>_day0_<ts>.md`）
- 问题场景：附问题总结报告（结论/现象/排障动作/根因/建议方案，`FAILED_` 前缀）
- trace `traces/d9_report.json`；ledger `d9_report`；`workflow.all_done=true`

---

## 问题总结报告触发条件（三选一即触发）

| 触发 | 结论 | 后续 |
|------|------|------|
| `day0.unfixable=true`（不可修复） | ❌ 无法适配 | 不发布，流程结束 |
| `day0.eval_unreachable=true`（精度无效） | ⚠ 精度无效 | 跳过性能与发布，流程结束 |
| `day0.repair` 非空但流程成功（修复过） | ✅ 已修复 | 正常发布，报告附修复记录 |

---

## 长任务执行协议（硬性，本流程全部长命令统一走）

1. 写任务命令文件（一条 docker exec，内容自由写无转义问题）：
   `mkdir -p /flagos-workspace/logs/tasks && cat > /flagos-workspace/logs/tasks/<TASK_ID>.cmd << 'CMD_EOF' ... CMD_EOF`
2. detached 启动（立即返回）：
   `docker exec -d ${CONTAINER} bash -c "cd /flagos-workspace/scripts && PATH=/opt/conda/bin:\$PATH python3 task_runner.py --cmd 'bash /flagos-workspace/logs/tasks/<TASK_ID>.cmd' --state /flagos-workspace/logs/tasks/<TASK_ID>.state --log /flagos-workspace/logs/tasks/<TASK_ID>.log --timeout <上限秒>"`
3. 短轮询（每 8 分钟一次）：`sleep 480 && docker exec ${CONTAINER} bash -c "cat .../<TASK_ID>.state 2>/dev/null; echo '---'; tail -3 .../<TASK_ID>.log"`
   - running → 继续；日志停增且进程消失 → 读日志诊断；done/error/timeout → 按终态处理
- **断点恢复**：启动前先查 state——running=接管轮询禁止重复启动；done/error=按终态处理
- 禁止 Bash(timeout=大数) 前台阻塞、禁止 TaskOutput 轮询

---

## 相关工具路径速查（容器内 /flagos-workspace/scripts/）

| 用途 | 工具 |
|------|------|
| 服务启动/等待 | start_service.sh（--mode flagos / --enforce-eager）、wait_for_service.sh |
| 冒烟测例 | smoke_test.py（--prompt '中国首都在哪'） |
| 环境检测 | inspect_env.py |
| 算子配置/定位 | apply_op_config.py（--mode custom --flagos-blacklist）、diagnose_ops.py |
| 精度评测/判定 | fast_gpqa.py、eval_wrapper.py、accuracy_compare.py（--nv-baseline） |
| 性能采集 | benchmark_runner.py（--output-name day0_performance） |
| 报告 | generate_day0_report.py |
| 状态 | update_context.py、task_runner.py |
