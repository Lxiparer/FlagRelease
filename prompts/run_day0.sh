#!/bin/bash
# FlagOS day0 快速适配验证 — 一键编排脚本
#
# 场景（用户 2026-08-13 定稿）：
#   给定镜像（必含 plugin+flaggems+flagtree 全组件）+ 本地权重路径，
#   全组件启动 → 首都冒烟测例（硬闸门）→ 全量 gpqa_diamond 精度 → 精度调优
#   → 性能采集 → 私有发布（Harbor + ModelScope + HuggingFace 全私有，tag -day0）。
#
# 用法:
#   bash prompts/run_day0.sh <镜像地址> <本地权重路径> <MODELSCOPE_TOKEN> <HF_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD> [--verbose]
#
# 示例:
#   bash prompts/run_day0.sh harbor.baai.ac.cn/flagrelease/qwen3:latest /mnt/data/models/Qwen3-8B ms_xxx hf_xxx harbor_user harbor_pass
#
# 段划分（每段一个独立 Claude 会话，段间由 day0_gate.py 确定性门控）:
#   段1: D1 容器准备 → D2 环境检测 → D3 全组件启动 → D4 首都冒烟测例（含排障循环）
#   段2: D5 精度评测（全量 gpqa_diamond vs NV 基线）→ D6 精度调优（关算子 ≤3 轮）
#   段3: D7 性能采集 → D8 私有发布 → D9 报告收尾
#
# 前置条件:
#   - Claude Code CLI 已安装 (claude 命令可用)
#   - Docker daemon 正在运行
#   - 当前目录为项目根目录
#   - 本地权重路径存在（用户显式提供，不做自动搜索/下载）

set -euo pipefail

# ========== Docker 前置检查 ==========
if ! docker ps &>/dev/null; then
    echo "错误: Docker daemon 未运行或无权限，请检查 Docker 状态"
    exit 1
fi

# ========== 宿主机 Python 依赖检查 ==========
if ! command -v python3 &>/dev/null; then
    echo "错误: python3 未安装，请先安装 Python 3"
    exit 1
fi
if ! python3 -c "import yaml" 2>/dev/null; then
    echo "[pre-flight] 安装宿主机 Python 依赖: pyyaml"
    pip3 install pyyaml -q 2>/dev/null || pip3 install pyyaml -q -i https://mirrors.aliyun.com/pypi/simple/ 2>/dev/null || \
    { echo "错误: 宿主机 pyyaml 安装失败"; exit 1; }
fi

# ========== 参数解析 ==========
if [ $# -lt 6 ]; then
    echo "用法: bash prompts/run_day0.sh <镜像地址> <本地权重路径> <MODELSCOPE_TOKEN> <HF_TOKEN> <HARBOR_USER> <HARBOR_PASSWORD> [--verbose]"
    echo ""
    echo "  day0 快速适配验证：全组件启动 → 首都冒烟 → 全量 gpqa_diamond 精度 → 调优 → 性能 → 私有发布"
    exit 1
fi

IMAGE="$1"
WEIGHT_PATH_HOST="$2"
MODELSCOPE_TOKEN="$3"
HF_TOKEN="$4"
HARBOR_USER="$5"
HARBOR_PASSWORD="$6"
VERBOSE=false
if [ $# -gt 6 ]; then
    for extra in "${@:7}"; do
        case "$extra" in
            --verbose) VERBOSE=true ;;
        esac
    done
fi

# ========== 权重路径校验（用户显式提供，必须存在） ==========
WEIGHT_PATH_HOST="$(echo "${WEIGHT_PATH_HOST}" | sed 's#/$##')"
if [ ! -d "${WEIGHT_PATH_HOST}" ]; then
    echo "错误: 本地权重路径不存在: ${WEIGHT_PATH_HOST}"
    exit 1
fi

# 模型名 = 权重路径 basename（发布命名/精度基线查表均用它）
MODEL="$(basename "${WEIGHT_PATH_HOST}")"
MODEL_SHORT_FOR_NAME="$(echo "${MODEL}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/-\+/-/g; s/^-//; s/-$//')"

# ========== 镜像存在性检查 + 拉取（确定性归 shell，agent 不参与） ==========
if ! docker image inspect "${IMAGE}" &>/dev/null; then
    echo "[镜像] 本地不存在 ${IMAGE}，尝试拉取..."
    if ! docker pull "${IMAGE}"; then
        echo "错误: 镜像拉取失败: ${IMAGE}（可先手动 docker pull 后重试）"
        exit 1
    fi
    echo "[镜像] 拉取完成: ${IMAGE}"
else
    echo "[镜像] 本地已存在: ${IMAGE}"
fi

# ========== 宿主机工作目录初始化（归档 + 创建） ==========
HOST_BASE="/data/flagos-workspace/${MODEL}"
if [ -d "${HOST_BASE}" ]; then
    HOST_HAS_HISTORY=0
    for d in results traces logs config reports eval; do
        if [ -d "${HOST_BASE}/${d}" ] && [ "$(ls -A "${HOST_BASE}/${d}" 2>/dev/null)" ]; then
            HOST_HAS_HISTORY=1; break
        fi
    done
    if [ "${HOST_HAS_HISTORY}" = "1" ]; then
        ARCHIVE_TS="$(date +%Y%m%d_%H%M%S)"
        HOST_ARCHIVE="${HOST_BASE}/archive/${ARCHIVE_TS}"
        mkdir -p "${HOST_ARCHIVE}"
        for d in results traces logs config reports eval; do
            if [ -d "${HOST_BASE}/${d}" ] && [ "$(ls -A "${HOST_BASE}/${d}" 2>/dev/null)" ]; then
                mv "${HOST_BASE}/${d}" "${HOST_ARCHIVE}/${d}"
            fi
        done
        echo "  宿主机历史数据已归档到: ${HOST_ARCHIVE}/"
    fi
fi
for d in logs config results traces; do
    mkdir -p "${HOST_BASE}/${d}"
done

TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_DIR="${HOST_BASE}/logs"
LOG_FILE="${LOG_DIR}/claude_day0_${TIMESTAMP}.log"
FULL_LOG="${LOG_DIR}/claude_day0_full_${TIMESTAMP}.log"
DEBUG_FILE="${LOG_DIR}/claude_day0_debug_${TIMESTAMP}.log"
PIPELINE_LOG="${LOG_DIR}/pipeline.log"
TERMINAL_LOG="${LOG_DIR}/terminal.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  FlagOS day0 快速适配验证                                     ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "  镜像:     ${IMAGE}"
echo "  权重路径: ${WEIGHT_PATH_HOST}"
echo "  模型名:   ${MODEL}"
echo "  工作目录: ${HOST_BASE}"
echo "  日志:     ${LOG_FILE}"
echo ""

# ========== 凭证 export（宿主机命令 + claude 进程共用） ==========
export MODELSCOPE_TOKEN HF_TOKEN HARBOR_USER HARBOR_PASSWORD

# 禁用实验性 beta 功能，避免第三方代理不支持 context_management 返回 400
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
# 禁用非核心 haiku 调用（session title 等），第三方代理通常无 haiku 权限会 403
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# ========== 部署权限白名单 + 动态注入模型特定权限 ==========
mkdir -p "${PROJECT_ROOT}/.claude" && cp "${PROJECT_ROOT}/settings.local.json" "${PROJECT_ROOT}/.claude/settings.local.json"
python3 -c "
import json, sys
model = sys.argv[1]
with open('${PROJECT_ROOT}/.claude/settings.local.json') as f:
    cfg = json.load(f)
rules = cfg.setdefault('permissions', {}).setdefault('allow', [])
for d in ['logs', 'config', 'results', 'traces']:
    rule = f'Bash(mkdir -p /data/flagos-workspace/{model}/{d})'
    if rule not in rules:
        rules.append(rule)
for rule in [
    f'Read(//data/flagos-workspace/{model}/**)',
    f'Bash(cat /data/flagos-workspace/{model}/*)',
    f'Bash(find /data/flagos-workspace/{model}/*)',
    f'Bash(tail /data/flagos-workspace/{model}/*)',
]:
    if rule not in rules:
        rules.append(rule)
with open('${PROJECT_ROOT}/.claude/settings.local.json', 'w') as f:
    json.dump(cfg, f, indent=2)
" "${MODEL}"
echo "  ✓ 已注入 ${MODEL} 模型特定权限规则"

# ========== 容器名确定性预生成（冲突必然追加时间戳，agent 只消费不判断） ==========
CONTAINER_NAME_PRE="${MODEL_SHORT_FOR_NAME}_day0"
if docker inspect --type=container "${CONTAINER_NAME_PRE}" &>/dev/null 2>&1; then
    CONTAINER_NAME_PRE="${MODEL_SHORT_FOR_NAME}_day0_$(date +%m%d_%H%M)"
    if docker inspect --type=container "${CONTAINER_NAME_PRE}" &>/dev/null 2>&1; then
        CONTAINER_NAME_PRE="${MODEL_SHORT_FOR_NAME}_day0_$(date +%m%d_%H%M%S)"
    fi
fi
echo "  容器名:   ${CONTAINER_NAME_PRE}（day0 禁止复用已有容器）"
echo ""

# ========== 段间状态读取 ==========
ledger_terminal() {
    # 判断 ledger 某步骤是否已进入终态（success/failed/skipped）——段幂等检查
    local STEP_PREFIX="$1"
    local CTX="${HOST_BASE}/config/context_snapshot.yaml"
    [ -f "${CTX}" ] || { echo "no"; return; }
    python3 -c "
import yaml
try:
    with open('${CTX}') as f:
        ctx = yaml.safe_load(f)
    ledger = ctx.get('workflow_ledger', {}).get('steps', [])
    items = ledger if isinstance(ledger, list) else list(ledger.values()) if isinstance(ledger, dict) else []
    for s in items:
        if isinstance(s, dict) and str(s.get('step','')).startswith('${STEP_PREFIX}') and s.get('status') in ('success','failed','skipped'):
            print('yes'); break
    else:
        print('no')
except Exception:
    print('no')
"
}

# ========== 公共 Prompt 块 ==========
COMMON_TOKENS=$(cat <<TOKENS_EOF

**宿主机凭证注入（2026-08-05 发布事故修复口径）**：
  HARBOR_USER/HARBOR_PASSWORD/MODELSCOPE_TOKEN/HF_TOKEN 已由编排层 export 到本
  Claude 进程的环境变量，宿主机直接执行的 python3 命令【禁止添加 env VAR=... 或
  /opt/conda/bin/python3 前缀】，直接按标准形态执行即可：
    python3 skills/flagos-release/tools/main.py --from-context ...
  长任务（发布等）detached 启动：python3 ... task_runner.py ... &（& 后台符后的
  task_runner 继承本进程环境变量；禁止 nohup/disown）
  （仅容器内命令才需要 docker exec -e 传凭证）
TOKENS_EOF
)

COMMON_DAY0_RULES=$(cat <<RULES_EOF

**day0 场景硬规则（优先级高于 CLAUDE.md 通用约束）**：
- 镜像默认必含 plugin+flaggems+flagtree 全组件；**无 V1 基线**：精度基线 = nv_baseline.yaml，性能无对比无闸门仅采集
- 算子控制**固定走 plugin env 路径**：VLLM_FL_FLAGOS_BLACKLIST（apply_op_config.py --mode custom --flagos-blacklist 生成 env_inline 启动前缀）。**禁止写控制文件**（plugin 下控制文件空转）
- **eager 仅限启动阶段**：--enforce-eager 只在服务起不来、关算子无效后使用
- **修复自主权**：排障定位手段与修复方式自由发挥；边界两条——改完必须重启+冒烟验证；diff 必须记录进 day0.repair。修复**不同步**上游
- 排障动作全程记录：day0.repair（round/action/ops|files/result/note）
- 本流程无 issue 提交环节，问题一律进问题总结报告（generate_day0_report.py）
RULES_EOF
)

COMMON_LONG_TASK=$(cat <<LONG_EOF

**长任务执行协议（硬性）**：所有长跑命令（服务等待、精度评测、benchmark）禁止
Bash(timeout=大数) 前台阻塞（10 分钟硬上限→会话被杀）、禁止 TaskOutput 轮询。三步：
1. 写任务命令文件：docker exec \${CONTAINER} bash -c "mkdir -p /flagos-workspace/logs/tasks && cat > /flagos-workspace/logs/tasks/<TASK_ID>.cmd << 'CMD_EOF' ... CMD_EOF"
2. detached 启动（立即返回）：docker exec -d \${CONTAINER} bash -c "cd /flagos-workspace/scripts && PATH=/opt/conda/bin:\$PATH python3 task_runner.py --cmd 'bash /flagos-workspace/logs/tasks/<TASK_ID>.cmd' --state /flagos-workspace/logs/tasks/<TASK_ID>.state --log /flagos-workspace/logs/tasks/<TASK_ID>.log --timeout <上限秒>"
3. 短轮询（每 8 分钟）：sleep 480 && docker exec \${CONTAINER} bash -c "cat .../<TASK_ID>.state 2>/dev/null; echo '---'; tail -3 .../<TASK_ID>.log"
   running→继续；done/error/timeout→按终态处理
断点恢复：启动前先查 .state——running=直接接管轮询禁止重复启动；done/error=按终态处理
LONG_EOF
)

FILTER_FLAGS=""
$VERBOSE && FILTER_FLAGS="--verbose"

# ================================================================
# 段1: D1 容器准备 + D2 环境检测 + D3 全组件启动 + D4 首都冒烟测例
# ================================================================
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│  段1 准备 + 全组件启动 + 首都冒烟测例  (D1→D2→D3→D4)        │"
echo "└──────────────────────────────────────────────────────────────┘"
SEG1_START_TS=$(date +%s)

SEG1_IDEMPOTENT="no"
if [ -f "${HOST_BASE}/config/context_snapshot.yaml" ]; then
    SEG1_IDEMPOTENT=$(ledger_terminal "d4")
fi

if [ "${SEG1_IDEMPOTENT}" = "yes" ]; then
    echo "  ✓ 段1 已完成（ledger d4_smoke_test 终态），跳过段1 Claude 调用"
else
PROMPT_SEG1="day0 快速适配验证 — 段1。

**变量定义**：IMAGE=${IMAGE}，WEIGHT_PATH_HOST=${WEIGHT_PATH_HOST}，MODEL=${MODEL}
CONTAINER_NAME=${CONTAINER_NAME_PRE}（编排层已确定，必须原样使用，禁止自行生成/修改）
CONTAINER_MODEL_PATH=${WEIGHT_PATH_HOST}（挂载到容器内同路径）
${COMMON_TOKENS}
${COMMON_DAY0_RULES}
${COMMON_LONG_TASK}

**执行前必读**：skills/flagos-day0/SKILL.md（本流程唯一编排指令源，含每步命令模板与排障决策树）。
**禁止执行段2/段3 内容（D5-D9）**。

## 步骤 D1 — 容器准备
- 镜像已由编排层确保存在于本地，**禁止执行 docker pull**
- 检测 GPU 厂商，按 skills/flagos-container-preparation/SKILL.md 中对应模板 docker run（仅替换变量值）
- NVIDIA 模板（严格执行）：
  docker run -itd --name=\${CONTAINER_NAME} --gpus=all --network=host -v ${WEIGHT_PATH_HOST}:${CONTAINER_MODEL_PATH} -v ${HOST_BASE}:/flagos-workspace ${IMAGE}
- 模板失败 → 修正变量重试 → docker inspect 同类容器借鉴重试一次 → 仍失败终止
- **绝对禁止复用已存在容器**（重名已由编排层追加时间戳规避）
- 部署工具（day0 模板初始化）：
  bash skills/flagos-container-preparation/tools/setup_workspace.sh \${CONTAINER} ${MODEL} --context-template=context_day0.template.yaml
- 写 context（day0.entry.*/model.*/container.*/image.*/workspace.*）+ trace traces/d1_container_preparation.json + ledger d1

## 步骤 D2 — 环境检测（仅采集信息，无分支路由）
- docker exec \${CONTAINER} bash -c \"PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/inspect_env.py --json\"
- 确认三组件齐全（has_plugin=true、has_flagtree=true、flaggems 版本非空）。day0 镜像默认必为全组件，
  若缺失 → day0.unfixable=true + 问题总结报告终止（详见 SKILL.md）
- 空闲 GPU 检测 + calc_tp_size.py 推 TP + runtime.thinking_model（评测预算）
- trace traces/d2_environment_inspection.json + ledger d2

## 步骤 D3 — 全组件服务启动
- 启动（长任务协议 <TASK_ID>=startup_day0）：
  docker exec \${CONTAINER} bash -c \"cd /flagos-workspace/scripts && bash start_service.sh --mode flagos\"
  随后 wait_for_service.sh --port 8000 --model-name '${MODEL}' --timeout 180 --max-timeout 5760 --mode flagos --log-path /flagos-workspace/logs/startup_flagos.log（task_runner --timeout 6000）
- 启动后强制检查运行时算子列表 /tmp/flaggems_enable_oplist.txt（唯一权威来源），记录 service.enable_oplist_*
- trace traces/d3_service_startup.json + ledger d3

## 步骤 D4 — 首都冒烟测例（硬闸门）
- docker exec \${CONTAINER} bash -c \"PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/smoke_test.py --port 8000 --model-name '${MODEL}' --prompt '中国首都在哪' --json\"
- 通过 → day0.smoke.passed=true + workflow.smoke_ok=true + 记录 answer
- 不通过/启动失败 → 排障决策树（SKILL.md）：优先关算子（env blacklist 累计）→ 启动阶段可 --enforce-eager
  → 组件探索/轻量修复（自由发挥，diff 记 day0.repair）→ 不可修复 day0.unfixable=true
- 每轮排障 append day0.repair；冒烟重试不限轮次但必须可归因
- trace traces/d4_smoke_test.json + ledger d4

**完成步骤 D4 后**：
1. docker cp \${CONTAINER}:/flagos-workspace/shared/context.yaml ${HOST_BASE}/config/context_snapshot.yaml
2. 输出 \"[段1] D1-D4 完成，context 已同步\" 后**立即停止所有操作**

**进度输出**：步骤开始/完成输出 [步骤1]-[步骤4] 标记（对应 D1-D4），关键命令后 ✓/✗ 摘要。"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 段1 Claude 会话启动..."
    ( cd "${PROJECT_ROOT}" && claude -p "${PROMPT_SEG1}" \
        --permission-mode auto \
        --output-format stream-json \
        --verbose \
        --debug-file "${DEBUG_FILE}.seg1" \
        --max-turns 400 \
        2>&1 | tee -a "${LOG_FILE}" \
             | tee >(python3 "${SCRIPT_DIR}/stream_to_debug_log.py" >> "${FULL_LOG}") \
             | python3 "${SCRIPT_DIR}/stream_filter.py" --pipeline-log "${PIPELINE_LOG}" --terminal-log "${TERMINAL_LOG}" --start-step 1 --cost-file "${LOG_DIR}/seg1_cost.txt" --durations-file "${LOG_DIR}/seg1_durations.json" ${FILTER_FLAGS} || true )
fi

SEG1_END_TS=$(date +%s)
SEG1_MIN=$(( (SEG1_END_TS - SEG1_START_TS) / 60 ))
SEG1_SEC=$(( (SEG1_END_TS - SEG1_START_TS) % 60 ))
echo "[段1] 耗时 ${SEG1_MIN}m ${SEG1_SEC}s"

# ---- 段1→段2 门控：冒烟硬闸门 ----
if [ ! -f "${HOST_BASE}/config/context_snapshot.yaml" ]; then
    echo "错误: 段1未产出 context_snapshot.yaml，终止"
    exit 1
fi
GATE_SMOKE_OUT=$(python3 "${SCRIPT_DIR}/day0_gate.py" --context "${HOST_BASE}/config/context_snapshot.yaml" --check smoke --json 2>&1) || GATE_SMOKE_RC=$?
GATE_SMOKE_RC=${GATE_SMOKE_RC:-0}
echo "${GATE_SMOKE_OUT}"
if [ "${GATE_SMOKE_RC}" != "0" ]; then
    echo "[门控] 冒烟硬闸门未通过 → 跳过段2/段3，直接问题总结报告收尾"
    SKIP_TO_REPORT=1
else
    SKIP_TO_REPORT=0
fi

if [ "${SKIP_TO_REPORT}" = "0" ]; then
# ================================================================
# 段2: D5 精度评测 + D6 精度调优
# ================================================================
echo ""
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│  段2 精度评测 + 精度调优  (D5→D6)                            │"
echo "└──────────────────────────────────────────────────────────────┘"
SEG2_START_TS=$(date +%s)

SEG2_IDEMPOTENT="no"
if [ -f "${HOST_BASE}/config/context_snapshot.yaml" ]; then
    SEG2_IDEMPOTENT=$(ledger_terminal "d6")
fi
# d6 可能被跳过（精度一次达标），d5 终态即视为段2完成
if [ "${SEG2_IDEMPOTENT}" = "no" ]; then
    SEG2_IDEMPOTENT=$(ledger_terminal "d5")
fi

SEG_CTR=""
if [ -f "${HOST_BASE}/config/context_snapshot.yaml" ]; then
    SEG_CTR=$(python3 -c "
import yaml
try:
    with open('${HOST_BASE}/config/context_snapshot.yaml') as f:
        ctx = yaml.safe_load(f)
    print(ctx.get('container', {}).get('name', ''))
except: print('')
")
fi
if [ -z "${SEG_CTR}" ] || ! docker inspect --type=container "${SEG_CTR}" &>/dev/null 2>&1; then
    echo "错误: 容器 ${SEG_CTR:-未知} 不可用，无法继续段2"
    exit 1
fi

if [ "${SEG2_IDEMPOTENT}" = "yes" ]; then
    echo "  ✓ 段2 已完成（ledger d5/d6 终态），跳过段2 Claude 调用"
else
PROMPT_SEG2="day0 快速适配验证 — 段2。

**变量定义**：CONTAINER=${SEG_CTR}，MODEL=${MODEL}
${COMMON_TOKENS}
${COMMON_DAY0_RULES}
${COMMON_LONG_TASK}

**前段状态**：段1（D1-D4）已完成，冒烟硬闸门已过，容器 ${SEG_CTR} 就绪。
**禁止**回头重做 D1-D4，**禁止**执行段3内容（D7-D9）。
**执行前必读**：skills/flagos-day0/SKILL.md 步骤 D5/D6。

## 步骤 D5 — 精度评测（全量 gpqa_diamond，基线 = NV）
- 长任务协议（<TASK_ID>=day0_eval），cmd 文件内容：
  cd /flagos-workspace/scripts
  python3 eval_wrapper.py --eval-cmd 'python3 fast_gpqa.py --config fast_gpqa_config.yaml --dataset gpqa_diamond --limit 0 --output /flagos-workspace/results/day0_gpqa_result.json' --service-log /flagos-workspace/logs/startup_flagos.log --stall-timeout 300 --max-timeout 86400
- **必须传 --limit 0（全量 198 题；不传默认只跑 50 题）**；task_runner --timeout 86400。评测耗时长（thinking 模型 8h+）是预算内预期，禁止因耗时截断/跳过
- 评测期间服务崩溃 → eval_wrapper 报 service_crash → 重启服务后重试评测
- 完成后 NV 基线判定：
  docker exec \${CONTAINER} bash -c \"PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/accuracy_compare.py --v2 /flagos-workspace/results/day0_gpqa_result.json --nv-baseline '${MODEL}' --metric gpqa_diamond --output /flagos-workspace/results/accuracy_compare_day0.json --json\"
- rel_drop≤5% → day0.accuracy_ok=true + workflow.accuracy_ok=true；NV 基线缺失 → day0.eval_unreachable=true + 问题总结报告
- trace traces/d5_accuracy_eval.json + ledger d5

## 步骤 D6 — 精度调优（不达标时，≤3 轮）
- 定位问题算子：evalscope 留存的逐题回复/结果分析（自主发挥）→ apply_op_config.py --mode custom --flagos-blacklist（累计）→ 重启 → 冒烟确认 → 重测精度
- 每轮记录关闭算子与分数变化：day0.repair + eval.excluded_ops_accuracy；达标即停
- 3 轮无效 → 非算子因素判断 → day0.eval_unreachable=true + 问题总结报告 → 跳过性能 → 结束
- 评测报错/不可得 → 读 logs/_last_error.json 排查重试 1 次 → 仍失败 eval_unreachable=true
- trace traces/d6_accuracy_tuning.json + ledger d6（未触发调优则置 skipped）

**完成步骤 D6 后**：
1. docker cp \${CONTAINER}:/flagos-workspace/shared/context.yaml ${HOST_BASE}/config/context_snapshot.yaml
2. 输出 \"[段2] D5-D6 完成，context 已同步\" 后**立即停止所有操作**

**进度输出**：步骤开始/完成输出 [步骤5]-[步骤6] 标记（对应 D5-D6）。"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 段2 Claude 会话启动..."
    ( cd "${PROJECT_ROOT}" && claude -p "${PROMPT_SEG2}" \
        --permission-mode auto \
        --output-format stream-json \
        --verbose \
        --debug-file "${DEBUG_FILE}.seg2" \
        --max-turns 500 \
        2>&1 | tee -a "${LOG_FILE}" \
             | tee >(python3 "${SCRIPT_DIR}/stream_to_debug_log.py" >> "${FULL_LOG}") \
             | python3 "${SCRIPT_DIR}/stream_filter.py" --pipeline-log "${PIPELINE_LOG}" --terminal-log "${TERMINAL_LOG}" --start-step 5 --cost-file "${LOG_DIR}/seg2_cost.txt" --load-durations "${LOG_DIR}/seg1_durations.json" --durations-file "${LOG_DIR}/seg2_durations.json" ${FILTER_FLAGS} || true )
fi

SEG2_END_TS=$(date +%s)
SEG2_MIN=$(( (SEG2_END_TS - SEG2_START_TS) / 60 ))
SEG2_SEC=$(( (SEG2_END_TS - SEG2_START_TS) % 60 ))
echo "[段2] 耗时 ${SEG2_MIN}m ${SEG2_SEC}s"

# ---- 段2→段3 门控：精度达标才做性能 ----
GATE_ACC_OUT=$(python3 "${SCRIPT_DIR}/day0_gate.py" --context "${HOST_BASE}/config/context_snapshot.yaml" --check accuracy --json 2>&1) || GATE_ACC_RC=$?
GATE_ACC_RC=${GATE_ACC_RC:-0}
echo "${GATE_ACC_OUT}"
if [ "${GATE_ACC_RC}" != "0" ]; then
    echo "[门控] 精度门控未通过 → 跳过段3（性能/发布），直接问题总结报告收尾"
    SKIP_TO_REPORT=1
else
    SKIP_TO_REPORT=0
fi
fi  # end if SKIP_TO_REPORT (smoke gate)

# ================================================================
# 段3: D7 性能采集 + D8 私有发布 + D9 报告收尾
# ================================================================
if [ "${SKIP_TO_REPORT}" = "0" ]; then
echo ""
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│  段3 性能采集 + 私有发布 + 报告收尾  (D7→D8→D9)              │"
echo "└──────────────────────────────────────────────────────────────┘"
SEG3_START_TS=$(date +%s)

SEG3_IDEMPOTENT="no"
if [ -f "${HOST_BASE}/config/context_snapshot.yaml" ]; then
    SEG3_IDEMPOTENT=$(ledger_terminal "d8")
fi

SEG_CTR=$(python3 -c "
import yaml
try:
    with open('${HOST_BASE}/config/context_snapshot.yaml') as f:
        ctx = yaml.safe_load(f)
    print(ctx.get('container', {}).get('name', ''))
except: print('')
")
if [ -z "${SEG_CTR}" ] || ! docker inspect --type=container "${SEG_CTR}" &>/dev/null 2>&1; then
    echo "错误: 容器 ${SEG_CTR:-未知} 不可用，无法继续段3"
    exit 1
fi

if [ "${SEG3_IDEMPOTENT}" = "yes" ]; then
    echo "  ✓ 段3 主流程已完成（ledger d8_release 终态），跳过段3 Claude 调用"
else
PROMPT_SEG3="day0 快速适配验证 — 段3。

**变量定义**：CONTAINER=${SEG_CTR}，MODEL=${MODEL}
${COMMON_TOKENS}
${COMMON_DAY0_RULES}
${COMMON_LONG_TASK}

**前段状态**：段1/段2 已完成，精度达标（day0.accuracy_ok=true），可进入性能。
**禁止**回头重做 D1-D6。
**执行前必读**：skills/flagos-day0/SKILL.md 步骤 D7/D8/D9。

## 步骤 D7 — 性能评测（单轮采集，无对比无闸门无调优）
- 长任务协议（<TASK_ID>=day0_bench）：benchmark_runner.py 单轮 quick
  （4k_input_1k_output 并发 64，--output-name day0_performance，参数以 --help 为准）
- 结果写 perf.*；**不做 ratio 计算、不做性能调优**
- trace traces/d7_performance.json + ledger d7

## 步骤 D8 — 私有发布（Harbor + MS + HF 全私有，tag -day0）
- docker cp \${CONTAINER}:/flagos-workspace/shared/context.yaml ${HOST_BASE}/config/context_snapshot.yaml
- 宿主机长任务协议（<TASK_ID>=release_day0，可能数小时）：
  python3 skills/flagos-release/tools/main.py --from-context ${HOST_BASE}/config/context_snapshot.yaml --version-tag day0
- **不用 --only-harbor**（day0 私有发布 = Harbor + ModelScope + HuggingFace 三处全私有不公开）
- 完成回传 context_final.yaml，写 release.* 与 workflow.released=true
- trace traces/d8_release.json + ledger d8

## 步骤 D9 — 报告收尾
- 生成 day0 报告（常规报告；若 day0.repair 非空或问题触发则附问题总结报告）：
  python3 shared/generate_day0_report.py --context-yaml ${HOST_BASE}/config/context_snapshot.yaml --results-dir ${HOST_BASE}/results --json
- trace traces/d9_report.json + ledger d9 + workflow.all_done=true
- 输出 \"[段3] D7-D9 完成\" 后**立即停止所有操作**

**进度输出**：步骤开始/完成输出 [步骤7]-[步骤9] 标记（对应 D7-D9）。"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 段3 Claude 会话启动..."
    ( cd "${PROJECT_ROOT}" && claude -p "${PROMPT_SEG3}" \
        --permission-mode auto \
        --output-format stream-json \
        --verbose \
        --debug-file "${DEBUG_FILE}.seg3" \
        --max-turns 400 \
        2>&1 | tee -a "${LOG_FILE}" \
             | tee >(python3 "${SCRIPT_DIR}/stream_to_debug_log.py" >> "${FULL_LOG}") \
             | python3 "${SCRIPT_DIR}/stream_filter.py" --pipeline-log "${PIPELINE_LOG}" --terminal-log "${TERMINAL_LOG}" --start-step 7 --cost-file "${LOG_DIR}/seg3_cost.txt" --load-durations "${LOG_DIR}/seg2_durations.json" --durations-file "${LOG_DIR}/seg3_durations.json" ${FILTER_FLAGS} || true )
fi

SEG3_END_TS=$(date +%s)
SEG3_MIN=$(( (SEG3_END_TS - SEG3_START_TS) / 60 ))
SEG3_SEC=$(( (SEG3_END_TS - SEG3_START_TS) % 60 ))
echo "[段3] 耗时 ${SEG3_MIN}m ${SEG3_SEC}s"
fi  # end if SKIP_TO_REPORT

# ================================================================
# 终局：确定性报告兜底（不依赖 Claude 是否记得执行）
# ================================================================
echo ""
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│  报告兜底（确定性生成）                                      │"
echo "└──────────────────────────────────────────────────────────────┘"
if [ -f "${HOST_BASE}/config/context_snapshot.yaml" ]; then
    python3 "${PROJECT_ROOT}/shared/generate_day0_report.py" \
        --context-yaml "${HOST_BASE}/config/context_snapshot.yaml" \
        --results-dir "${HOST_BASE}/results" --json 2>&1 | tail -20 || \
        echo "  ⚠ generate_day0_report.py 执行失败，请查看 ${FULL_LOG}"
else
    echo "  ⚠ 无 context_snapshot.yaml，跳过报告生成"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  day0 流程结束                                                ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "  模型:      ${MODEL}"
echo "  工作目录:  ${HOST_BASE}"
echo "  报告:      ${HOST_BASE}/results/*.md"
echo "  日志:      ${LOG_FILE}"
if [ -f "${HOST_BASE}/config/context_snapshot.yaml" ]; then
python3 -c "
import yaml
try:
    with open('${HOST_BASE}/config/context_snapshot.yaml') as f:
        ctx = yaml.safe_load(f)
    d = ctx.get('day0', {}) or {}
    wf = ctx.get('workflow', {}) or {}
    print(f\"  冒烟:      {'✓ 通过' if d.get('smoke',{}).get('passed') else '✗ 未通过'}\")
    print(f\"  精度:      {'✓ 达标' if d.get('accuracy_ok') else '✗ 未达标/无效'}\")
    print(f\"  发布:      {'✓ 完成' if wf.get('released') else '- 未发布'}\")
    if d.get('unfixable'): print(f\"  结论:      不可修复 → 问题总结报告\")
    elif d.get('eval_unreachable'): print(f\"  结论:      精度无效 → 问题总结报告（跳过性能/发布）\")
    elif d.get('accuracy_ok') and wf.get('released'): print(f\"  结论:      流程完成（私有发布 tag -day0）\")
    else: print(f\"  结论:      未完成，请查看报告与日志\")
except Exception as e:
    print(f\"  (context 解析失败: {e})\")
"
fi
echo ""
