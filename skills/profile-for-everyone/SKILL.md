---
name: profile-for-everyone
description: 自动 profile（vendor / gems 双跑）→ probe → compare 出交互式 dashboard → 生成给 Claude Code 的算子级 triage skill；用于定位 FlagGems 算子相比 vendor 为何更慢
version: 1.0.0
triggers:
  - 算子 profiling
  - operator profiling
  - 性能定位
  - gems vs vendor
  - 算子对比
  - why gems slower
  - trace 对比
  - profile-for-everyone
depends_on: []
next_skill: null
provides:
  - profile.dashboard_path
  - profile.src_dir
  - profile.triage_entry
  - profile.gems_slower_targets
---

# profile-for-everyone Skill

把 vLLM 在 vendor / gems 两侧的 GPU 行为抓下来并逐算子对比，产出一个自包含的 dashboard，
并打包成可直接交给 Claude Code 的 triage 材料：按"绝对耗时差"排好序的 gems-慢算子、
进入对应容器的方式、以及结构化查询命令，用来顺着 CPU 调用栈找到算子源码、解释为何更慢、给出并验证修复。

本 skill 在**宿主机**运行，自行管理容器与 vLLM（不依赖其它 skill）。

## 流程（4 个阶段）

| 阶段 | 含义 | 产物 |
|-----:|------|------|
| 1 | 自动 profile：在 vendor / gems 容器内起 vLLM + torch profiler，导出 trace | `traceSource_both/{gems,vendor}.pt.trace.json.gz` |
| 2 | 自动 probe：解析 trace，构建语义树 / CPU 调用栈树 | `<ReleaseName>/src/{gems,vendor}/...` |
| 3 | 自动 compare + dashboard | `<ReleaseName>/compare_dashboard.html` |
| 4 | 自动生成 Agent triage（含 skill bundle） | `<ReleaseName>/agent/CLAUDE_TRIAGE.md` 等 |

注意：probe(2) 与 compare(3) 由同一脚本产出，绑定一起跑。

## 运行方式

```bash
cd skills/profile-for-everyone/tools

# 一条龙（profile → probe → compare → skills）。--task-dir 是产出根目录。
python3 run_full_pipeline.py --task-dir /abs/work/run1 --decode-idx 30

# 只跑部分阶段：1=profile 2=probe 3=compare 4=skills
python3 run_full_pipeline.py --task-dir /abs/work/run1 --stages 1-3      # 不生成 skills
python3 run_full_pipeline.py --task-dir /abs/work/run1 --stages 4        # 续跑：补生成 skills
python3 run_full_pipeline.py --task-dir /abs/work/run1 --skip-profile    # 续跑：用已有 trace 接后续

# 后台长跑（实时日志）
PYTHONUNBUFFERED=1 nohup python3 run_full_pipeline.py --task-dir /abs/work/run1 --decode-idx 30 \
  < /dev/null > /abs/work/run1/pipeline.log 2>&1 &
```

续跑无需传中间文件：产物按 `--task-dir` + `release_name` 推导的固定路径落盘，脚本自行查找。

## 输出结构

`--task-dir` 下，成果收进一个 `<ReleaseName>/` 文件夹（名字取自配置里的 `release_name`），打开只有三样：

```
<task_dir>/
├── traceSource_both/         # profile 产物（trace），大文件，放在成果文件夹外
├── profile_logs/             # profile 运行日志
└── <ReleaseName>/            # 成果文件夹
    ├── compare_dashboard.html    # 自包含，浏览器直接打开
    ├── src/                       # 源文件：gems/ + vendor/（cpu_stack.html、trace 切片等）
    └── agent/                     # 交给 Claude Code：CLAUDE_TRIAGE.md、claude_skills/、
                                   #   agent_*.json、compare_*.csv
```

## 配置

用交互式工具或直接改 JSON，需要设置的主要是**模型路径 / 镜像 / release 名**：

```bash
python3 tools/auto_profile/diyConfig.py        # 中文菜单；7=预览生效配置，0=保存退出
```

- `tools/auto_profile/local_required.json` → `targets.{vendor,gems}.model_path`（模板里是占位 `/path/to/models/your-model`，改成你的模型目录）
- `tools/auto_profile/upstream.json` → `release_name`（决定成果文件夹名）、两侧 `image`
- `tools/auto_profile/*_docker_run.sh` → `-v` 挂载（把含模型的宿主目录挂进容器）、GPU 标志（NV 用 `--gpus all`，其它厂商改这里）。容器名由 `release_name` 自动派生，无需手填。

## trace_processor_shell（重要）

probe 阶段依赖 perfetto 的 `trace_processor_shell` 二进制，路径默认 `tools/probe/trace_processor_shell`。
**本仓库按设计直接绑定一个二进制**（避免运行时下载经常断连）。请确保它是**你目标机器平台**的版本
（Hygon / NV 服务器一般是 Linux x86-64，`file` 应显示 `ELF 64-bit ... x86-64`），不对就替换它并 `chmod +x`。

> 实在需要时也可临时下载：`curl -fL https://get.perfetto.dev/trace_processor -o tools/probe/trace_processor_shell && chmod +x tools/probe/trace_processor_shell`
> （注意 `/tmp` 若被挂成 `noexec`，需把工作目录放到非 noexec 的盘）。

## 上下文与日志（重要 — 给编排层 Claude Code）

本 skill 的 1–3 阶段是**纯数据处理、不需要 LLM**；只有第 4 阶段产出的材料是给 Claude Code 用的。
为避免污染 / 撑爆上下文（profile 阶段 vLLM 日志可能上千行），约定：

- **跑 pipeline 时务必把输出重定向到日志文件**（`> pipeline.log 2>&1`），不要把过程日志流进对话上下文。Claude Code 只需在结束后读最后几行汇总或读产物文件，不要 `tail` 整个日志。
- **分析 / 优化阶段只读紧凑产物**：`agent/CLAUDE_TRIAGE.md`，并用结构化查询（`semantic_perf_query.py describe --rank N` —— 每次只取**一个**算子的聚焦数据）。**不要** `cat` 整个 `*.log` / `compare_*.csv` / `agent_semantic_index.json` 进上下文。
- **强烈建议把"跑 pipeline"与"分析+算子优化"拆成不同会话**：先（可不用 Claude Code，直接命令行）跑完 1–4，再新开一个干净会话，只喂 `CLAUDE_TRIAGE.md` 去做优化，保证优化阶段上下文不被 profiling 日志挤占。

## 交付给 Claude Code

跑完后把 `<ReleaseName>/agent/CLAUDE_TRIAGE.md` 作为入口交给 Claude Code，它据此：
进入对应容器（gems / vendor）、按排好序的 gems-慢算子用结构化查询定位源码、分析并修复。常用命令：

```bash
python3 tools/agent/semantic_perf_query.py list-targets --run-dir <ReleaseName> --sort delta --limit 30
python3 tools/agent/semantic_perf_query.py describe     --run-dir <ReleaseName> --rank 1
python3 tools/agent/container_source_query.py show-context --run-dir <ReleaseName>   # 只读进容器看源码
```

## 主要文件

- `tools/run_full_pipeline.py` — 编排入口（4 阶段 / `--stages` / 续跑）
- `tools/auto_profile/` — 自动 profile（`profile_runner.py`、`diyConfig.py`、配置与各侧 `.sh`）
- `tools/probe/` — probe + 渲染（`probe_trace.py`、`render_trace_tree.py`、`schema.json`、`trace_processor_shell`）
- `tools/probe_and_compare.py` + `tools/clean_compare_and_generate_dashboard.py` — probe + compare + dashboard
- `tools/agent/` — triage 生成（`generate_skills.py`）+ 查询引擎 + 仓库 skill 模板 `claude_skills/gpu-semantic-triage/`
