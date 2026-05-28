#!/bin/bash
# Plugin Fix Pipeline — 一键启动脚本（三段式）
# 修复已有 plugin 环境的镜像，使其能正常启动推理服务，采集数据并打包发布。
# 分为三段独立 Claude 会话：段1(准备+检测) → 段2(修复) → 段3(评测+发布)
#
# 用法:
#   bash prompts/run_plugin_fix_pipeline.sh <镜像地址> <模型名> <HARBOR_USER> <HARBOR_PASSWORD> [--proxy proxy1,proxy2,...] [--max-fix-rounds 5] [--model-path <路径>] [--verbose]
#
# 示例:
#   bash prompts/run_plugin_fix_pipeline.sh harbor.baai.ac.cn/flagrelease/qwen3-plugin:latest Qwen3-8B harbor_user harbor_pass
#   bash prompts/run_plugin_fix_pipeline.sh harbor.baai.ac.cn/flagrelease/qwen3-plugin:latest Qwen3-8B harbor_user harbor_pass --proxy http://10.1.12.192:80 --max-fix-rounds 3
#
# 前置条件:
#   - Claude Code CLI 已安装 (claude 命令可用)
#   - Docker daemon 正在运行
#   - 当前目录为项目根目录

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

HOST_PY_DEPS=("yaml:pyyaml")
MISSING_PKGS=()
for dep in "${HOST_PY_DEPS[@]}"; do
    mod="${dep%%:*}"
    pkg="${dep##*:}"
    if ! python3 -c "import ${mod}" 2>/dev/null; then
        MISSING_PKGS+=("${pkg}")
    fi
done

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo "[pre-flight] 安装缺失的宿主机 Python 依赖: ${MISSING_PKGS[*]}"
    pip3 install "${MISSING_PKGS[@]}" -q 2>/dev/null || \
    pip3 install "${MISSING_PKGS[@]}" -q -i https://mirrors.aliyun.com/pypi/simple/ 2>/dev/null || \
    pip3 install "${MISSING_PKGS[@]}" -q -i https://pypi.tuna.tsinghua.edu.cn/simple/ 2>/dev/null || \
    { echo "错误: 宿主机 Python 依赖安装失败: ${MISSING_PKGS[*]}"; exit 1; }
fi

# ========== 参数解析 ==========
if [ $# -lt 4 ]; then
    echo "用法: $0 <镜像地址> <模型名> <HARBOR_USER> <HARBOR_PASSWORD> [--proxy proxy1,...] [--max-fix-rounds N] [--model-path <路径>] [--verbose]"
    exit 1
fi

IMAGE="$1"
MODEL="$2"
export HARBOR_USER="$3"
export HARBOR_PASSWORD="$4"
shift 4

MAX_FIX_ROUNDS=5
PROXY_LIST=""
MODEL_PATH=""
FILTER_FLAGS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --verbose) FILTER_FLAGS="--verbose"; shift ;;
        --proxy) PROXY_LIST="$2"; shift 2 ;;
        --max-fix-rounds) MAX_FIX_ROUNDS="$2"; shift 2 ;;
        --model-path)
            if [ -z "${2:-}" ]; then
                echo "错误: --model-path 需要指定路径"
                exit 1
            fi
            MODEL_PATH="$2"
            shift 2
            ;;
        *)
            echo "警告: 未知参数 '$1'，已忽略"
            shift
            ;;
    esac
done

if [ -z "$MODEL" ]; then
    echo "错误: 模型名为空"
    exit 1
fi

# ========== 模型路径搜索 ==========
MODEL_FOUND_ON_HOST=false
if [ -z "$MODEL_PATH" ]; then
    echo "[pre-flight] 搜索宿主机模型路径: ${MODEL} ..."
    SEARCH_JSON=$(python3 skills/flagos-container-preparation/tools/check_model_local.py \
        --model "${MODEL}" --no-download --output-json 2>/dev/null) || SEARCH_JSON=""

    MODEL_PATH=$(echo "$SEARCH_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('best_match') or '')
except:
    print('')
" 2>/dev/null) || MODEL_PATH=""

    if [ -n "$MODEL_PATH" ]; then
        echo "  ✓ 找到: ${MODEL_PATH}"
        MODEL_FOUND_ON_HOST=true
    else
        MODEL_SHORT=$(echo "${MODEL}" | sed 's|.*/||')
        MODEL_PATH="/data/models/${MODEL_SHORT}"
        mkdir -p "${MODEL_PATH}"
        echo "  ⚠ 宿主机未找到模型，预创建挂载目录: ${MODEL_PATH}"
    fi
else
    if [ ! -d "$MODEL_PATH" ]; then
        echo "错误: 指定的模型路径不存在: ${MODEL_PATH}"
        exit 1
    fi
    MODEL_FOUND_ON_HOST=true
    echo "[pre-flight] 使用指定模型路径: ${MODEL_PATH}"
fi
CONTAINER_MODEL_PATH="${MODEL_PATH}"

# ========== Banner ==========
echo "============================================================"
echo "  Plugin Fix Pipeline（三段式）"
echo "============================================================"
echo "  镜像: ${IMAGE}"
echo "  模型: ${MODEL}"
echo "  模型路径: ${MODEL_PATH}"
echo "  最大修复轮次: ${MAX_FIX_ROUNDS}"
echo "  输出目录: /data/plugin-fix-workspace/${MODEL}/"
echo "  权限: --permission-mode auto + settings.local.json allowlist"
echo "============================================================"
echo ""

# ========== 网络预检 ==========
if [ -z "$PROXY_LIST" ]; then
    CURRENT_PROXY="${https_proxy:-${http_proxy:-}}"
    [ -n "$CURRENT_PROXY" ] && PROXY_LIST="$CURRENT_PROXY"
fi

if [ -n "$PROXY_LIST" ]; then
    echo "[pre-flight] 网络连通性检测..."
    NETWORK_JSON=$(bash prompts/check_network.sh --proxies "${PROXY_LIST}" --json 2>/dev/null) || NETWORK_JSON=""
    if [ -n "$NETWORK_JSON" ]; then
        BEST_PROXY=$(echo "$NETWORK_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['recommended_proxy'])" 2>/dev/null) || BEST_PROXY=""
        ALL_FAILED=$(echo "$NETWORK_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['all_failed'])" 2>/dev/null) || ALL_FAILED="false"

        if [ "$ALL_FAILED" = "true" ] || [ "$ALL_FAILED" = "True" ]; then
            echo "  ✗ 所有代理均不可用，流程终止"
            exit 1
        fi

        if [ -n "$BEST_PROXY" ] && [ "$BEST_PROXY" != "direct" ]; then
            export http_proxy="$BEST_PROXY"
            export https_proxy="$BEST_PROXY"
            echo "  ✓ 推荐代理: ${BEST_PROXY}"
        elif [ "$BEST_PROXY" = "direct" ]; then
            echo "  ✓ 直连可用，无需代理"
        fi
    else
        echo "  ⚠ 网络检测脚本执行失败，使用默认代理继续"
    fi
    export FLAGOS_PROXY_LIST="${PROXY_LIST}"
    echo ""
fi

# ========== 构造通用 Prompt 片段 ==========
COMMON_TOKENS="
**凭证**（已通过 setup_workspace.sh 写入容器 /flagos-workspace/.env）：
  HARBOR_USER=${HARBOR_USER}
  HARBOR_PASSWORD=${HARBOR_PASSWORD}

**网络代理**：
  FLAGOS_PROXY_LIST=${FLAGOS_PROXY_LIST:-}
  FLAGOS_ACTIVE_PROXY=${http_proxy:-}
  所有需要外网的 docker exec 命令必须传入代理: docker exec -e http_proxy=${http_proxy:-} -e https_proxy=${https_proxy:-} ...
"

COMMON_PLAN_STEPS=$(cat <<PLAN_STEPS_EOF
**强制规则：每个步骤完成后立即同步 context_snapshot.yaml**
每完成一个步骤，在写入 trace 和更新容器内 context.yaml 之后，必须立即执行：
  docker cp \${CONTAINER}:/flagos-workspace/shared/context.yaml /data/plugin-fix-workspace/${MODEL}/config/context_snapshot.yaml

**强制规则：每个步骤完成后生成/更新报告**
  docker exec \${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/generate_report.py --output /flagos-workspace/results/report.md"
  docker exec \${CONTAINER} bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/generate_report.py --json --output /flagos-workspace/results/report.json"

**context.yaml 更新方式（必须使用工具脚本，禁止手写 Python）**：
  docker exec \${CONTAINER} bash -c "PATH=/opt/conda/bin:\\\$PATH python3 /flagos-workspace/scripts/update_context.py --set key.path=value --json"
  docker exec \${CONTAINER} bash -c "PATH=/opt/conda/bin:\\\$PATH python3 /flagos-workspace/scripts/update_context.py --ledger-update <step_key> --ledger-status success --ledger-notes '...' --json"

**服务等待策略（硬性）**：
- wait_for_service.sh 前台阻塞执行，Bash timeout=600000
- 禁止使用 TaskOutput 轮询
PLAN_STEPS_EOF
)

# ========== 部署权限白名单 ==========
mkdir -p .claude && cp settings.local.json .claude/settings.local.json

python3 -c "
import json, sys
model = sys.argv[1]
with open('.claude/settings.local.json') as f:
    cfg = json.load(f)
rules = cfg.setdefault('permissions', {}).setdefault('allow', [])
for d in ['logs', 'config', 'results', 'traces']:
    rule = f'Bash(mkdir -p /data/plugin-fix-workspace/{model}/{d})'
    if rule not in rules:
        rules.append(rule)
for rule in [
    f'Read(//data/plugin-fix-workspace/{model}/**)',
    f'Bash(cat /data/plugin-fix-workspace/{model}/*)',
    f'Bash(find /data/plugin-fix-workspace/{model}/*)',
    f'Bash(tail /data/plugin-fix-workspace/{model}/*)',
]:
    if rule not in rules:
        rules.append(rule)
with open('.claude/settings.local.json', 'w') as f:
    json.dump(cfg, f, indent=2)
" "${MODEL}"
echo "  ✓ 已注入 ${MODEL} 模型特定权限规则"

# ========== 宿主机历史数据归档 ==========
HOST_BASE="/data/plugin-fix-workspace/${MODEL}"
if [ -d "${HOST_BASE}" ]; then
    HOST_HAS_HISTORY=0
    for d in results traces logs config; do
        if [ -d "${HOST_BASE}/${d}" ] && [ "$(ls -A "${HOST_BASE}/${d}" 2>/dev/null)" ]; then
            HOST_HAS_HISTORY=1; break
        fi
    done
    if [ "${HOST_HAS_HISTORY}" = "1" ]; then
        ARCHIVE_TS="$(date +%Y%m%d_%H%M%S)"
        HOST_ARCHIVE="${HOST_BASE}/archive/${ARCHIVE_TS}"
        mkdir -p "${HOST_ARCHIVE}"
        for d in results traces logs config; do
            if [ -d "${HOST_BASE}/${d}" ] && [ "$(ls -A "${HOST_BASE}/${d}" 2>/dev/null)" ]; then
                mv "${HOST_BASE}/${d}" "${HOST_ARCHIVE}/${d}"
            fi
        done
        echo "  宿主机历史数据已归档到: ${HOST_ARCHIVE}/"
    fi
fi

for d in logs config results traces; do
    mkdir -p "/data/plugin-fix-workspace/${MODEL}/${d}"
done

# ========== 日志文件路径 ==========
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_DIR="/data/plugin-fix-workspace/${MODEL}/logs"
LOG_FILE="${LOG_DIR}/claude_plugin_fix_${TIMESTAMP}.log"
FULL_LOG="${LOG_DIR}/claude_full_${TIMESTAMP}.log"
DEBUG_FILE="${LOG_DIR}/claude_debug_${TIMESTAMP}.log"
PIPELINE_LOG="${LOG_DIR}/pipeline.log"
TERMINAL_LOG="${LOG_DIR}/terminal.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "日志文件:"
echo "  原始事件流: ${LOG_FILE}"
echo "  可读执行记录: ${FULL_LOG}"
echo "  流水线日志: ${PIPELINE_LOG}"
echo ""

export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# ===== 段间状态传递函数 =====
read_plugin_fix_context() {
    local MODEL_ARG="$1"
    local CTX="/data/plugin-fix-workspace/${MODEL_ARG}/config/context_snapshot.yaml"
    if [ ! -f "${CTX}" ]; then
        echo "ERROR: context_snapshot.yaml 不存在，前段可能未完成" >&2
        return 1
    fi
    python3 -c "
import yaml
with open('${CTX}') as f:
    ctx = yaml.safe_load(f)
ctr = ctx.get('container',{}).get('name','')
env = ctx.get('environment',{}).get('env_type','')
fix_status = ctx.get('plugin_fix',{}).get('final_status','')
service_ok = ctx.get('workflow',{}).get('service_ok', False)
model_path = ctx.get('model',{}).get('container_path','')
plugin_path = ctx.get('plugin',{}).get('source_path','')
gpu_count = ctx.get('gpu',{}).get('count','')
gpu_type = ctx.get('gpu',{}).get('type','')
port = ctx.get('service',{}).get('port', 8000)
print(f'{ctr}|{env}|{fix_status}|{service_ok}|{model_path}|{plugin_path}|{gpu_count}|{gpu_type}|{port}')
" 2>/dev/null
}

# ===== GPU 服务清理（退出时自动执行） =====
cleanup_gpu_services() {
    local ctr=""
    local ctx_file="/data/plugin-fix-workspace/${MODEL}/config/context_snapshot.yaml"
    if [ -f "${ctx_file}" ]; then
        ctr=$(python3 -c "
import yaml
try:
    with open('${ctx_file}') as f:
        ctx = yaml.safe_load(f)
    print(ctx.get('container',{}).get('name',''))
except: pass
" 2>/dev/null) || ctr=""
    fi
    if [ -n "${ctr}" ] && docker inspect --type=container "${ctr}" &>/dev/null; then
        echo ""
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 清理 GPU 资源：停止容器 ${ctr} 内的推理服务..."
        docker exec "${ctr}" bash -c "pkill -f 'vllm\|sglang\|flagscale' 2>/dev/null; sleep 2" 2>/dev/null && \
            echo "  ✓ 推理服务已停止，GPU 显存已释放" || \
            echo "  ⚠ 未发现运行中的推理服务（可能已停止）"
    fi
}
trap 'cleanup_gpu_services' EXIT

# ===== 全流程计时 =====
PIPELINE_START_TS=$(date +%s)

# ╔══════════════════════════════════════════════════════════════╗
# ║  段1/3  容器准备 + 环境检测  (步骤 1→2)                      ║
# ╚══════════════════════════════════════════════════════════════╝
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  段1/3  容器准备 + 环境检测  (步骤 1→2)                      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
SEG1_START_TS=$(date +%s)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 段1 开始"

PROMPT_SEG1="你正在执行 **Plugin Fix Pipeline 段1**，目标是准备容器并检测 plugin 环境。

## 基本信息

- 镜像: ${IMAGE}
- 模型名: ${MODEL}
- 宿主机模型路径: ${MODEL_PATH}
- 容器内模型路径: ${CONTAINER_MODEL_PATH}
${COMMON_TOKENS}

## 执行模式：计划优先（Plan-First）

在执行任何操作之前，先读取以下 SKILL.md 提取关键命令和参数：
- skills/flagos-container-preparation/SKILL.md（步骤1容器准备）
- skills/flagos-pre-service-inspection/SKILL.md（步骤2环境检测）

**重要：所有 /flagos-workspace 下的文件操作必须通过 docker exec 在容器内执行。**

## 工作流（本段只执行步骤 1+2）

### 步骤 1：容器准备

1. 从镜像 ${IMAGE} 创建容器（GPU 自动检测，参照 SKILL.md 中 GPU 厂商对应模板）
   - docker run 挂载路径：-v /data/plugin-fix-workspace/${MODEL}:/flagos-workspace
   - 模型挂载：-v ${MODEL_PATH}:${CONTAINER_MODEL_PATH}
2. 搜索模型权重：python3 skills/flagos-container-preparation/tools/check_model_local.py --model \"${MODEL}\" --mode container --container \$CONTAINER --output-json
3. 部署工具脚本：bash skills/flagos-container-preparation/tools/setup_workspace.sh \$CONTAINER ${MODEL} --context-template plugin_fix
4. 写入 context.yaml + traces/01_container_preparation.json

### 步骤 2：环境检测

1. docker exec \$CONTAINER bash -c \"PATH=/opt/conda/bin:\\\$PATH python3 /flagos-workspace/scripts/inspect_env.py --json\"
2. 确认 env_type=vllm_plugin_flaggems（如果不是，终止并报错）
3. 确认 plugin 已安装且可 import
4. 记录 plugin 源码路径到 context.yaml
5. 写入 traces/02_environment_inspection.json

## 通用规则

${COMMON_PLAN_STEPS}

**进度输出**：
- [步骤1] 容器准备 — 开始 / 完成 (耗时)
- [步骤2] 环境检测 — 开始 / 完成 (耗时)

**⚠ 段边界（硬性约束 — 最高优先级）**：
- 本段**只执行步骤1/2**，步骤2完成并同步 context_snapshot.yaml 后**必须立即停止**
- **绝对禁止**执行步骤3（Plugin修复）、步骤4、步骤5 或任何后续步骤
- 完成标志：输出 \"[段1] 步骤1/2全部完成，context 已同步\" 后立即停止"

claude -p "${PROMPT_SEG1}" \
    --permission-mode auto \
    --output-format stream-json \
    --verbose \
    --debug-file "${DEBUG_FILE}.seg1" \
    --max-turns 200 \
    2>&1 | tee "${LOG_FILE}" \
         | tee >(python3 "${SCRIPT_DIR}/stream_to_debug_log.py" > "${FULL_LOG}") \
         | python3 "${SCRIPT_DIR}/stream_filter.py" --pipeline-log "${PIPELINE_LOG}" --terminal-log "${TERMINAL_LOG}" --cost-file "${LOG_DIR}/seg1_cost.txt" ${FILTER_FLAGS} || true

# 段1完成 — 段间检查
SEG1_END_TS=$(date +%s)
SEG1_ELAPSED=$(( SEG1_END_TS - SEG1_START_TS ))
SEG1_MIN=$(( SEG1_ELAPSED / 60 ))
SEG1_SEC=$(( SEG1_ELAPSED % 60 ))
echo ""
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│  段1 完成 — 耗时 ${SEG1_MIN}m ${SEG1_SEC}s                                     │"
echo "└──────────────────────────────────────────────────────────────┘"

# 强制同步 context
CTX_FILE="/data/plugin-fix-workspace/${MODEL}/config/context_snapshot.yaml"
SHARED_CTX="/data/plugin-fix-workspace/${MODEL}/shared/context.yaml"
mkdir -p "$(dirname "${CTX_FILE}")"
if [ -f "${SHARED_CTX}" ]; then
    cp "${SHARED_CTX}" "${CTX_FILE}"
else
    # 尝试从容器 docker cp
    FALLBACK_CTR=$(grep -oP '(?<=容器 )\S+(?= 就绪)' "${PIPELINE_LOG}" 2>/dev/null | tail -1)
    if [ -n "${FALLBACK_CTR:-}" ] && docker inspect --type=container "${FALLBACK_CTR}" &>/dev/null; then
        docker cp "${FALLBACK_CTR}:/flagos-workspace/shared/context.yaml" "${CTX_FILE}" 2>/dev/null || true
    fi
fi

CTX_INFO=$(read_plugin_fix_context "${MODEL}" 2>/dev/null) || { echo "错误：段1未产出 context_snapshot.yaml，终止"; exit 1; }
SEG_CTR=$(echo "$CTX_INFO" | cut -d'|' -f1)
SEG_ENV=$(echo "$CTX_INFO" | cut -d'|' -f2)
SEG_MODEL_PATH=$(echo "$CTX_INFO" | cut -d'|' -f5)
SEG_PLUGIN_PATH=$(echo "$CTX_INFO" | cut -d'|' -f6)
SEG_GPU_COUNT=$(echo "$CTX_INFO" | cut -d'|' -f7)
SEG_GPU_TYPE=$(echo "$CTX_INFO" | cut -d'|' -f8)
SEG_PORT=$(echo "$CTX_INFO" | cut -d'|' -f9)

if [ -z "$SEG_CTR" ]; then
    echo "错误：段1未产出容器名，终止"
    exit 1
fi

echo "  容器名: ${SEG_CTR}"
echo "  环境类型: ${SEG_ENV}"
echo "  Plugin 路径: ${SEG_PLUGIN_PATH}"
echo ""

if [ "$SEG_ENV" != "vllm_plugin_flaggems" ]; then
    echo "错误：环境类型不是 vllm_plugin_flaggems（实际: ${SEG_ENV}），终止"
    exit 1
fi

# ╔══════════════════════════════════════════════════════════════╗
# ║  段2/3  Plugin 修复循环  (步骤 3)                             ║
# ╚══════════════════════════════════════════════════════════════╝
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  段2/3  Plugin 修复循环  (步骤 3)                             ║"
echo "╚══════════════════════════════════════════════════════════════╝"
SEG2_START_TS=$(date +%s)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 段2 开始"

PROMPT_SEG2="你正在执行 **Plugin Fix Pipeline 段2**，目标是修复 plugin 使其能正常启动推理服务。

## 基本信息（段1已完成，以下为段间传递的参数）

- 容器名: ${SEG_CTR}
- 模型名: ${MODEL}
- 容器内模型路径: ${SEG_MODEL_PATH}
- Plugin 源码路径: ${SEG_PLUGIN_PATH}
- GPU: ${SEG_GPU_COUNT} x ${SEG_GPU_TYPE}
- 服务端口: ${SEG_PORT}
- 最大修复轮次: ${MAX_FIX_ROUNDS}
${COMMON_TOKENS}

**变量定义**：CONTAINER=${SEG_CTR}

## 执行模式：计划优先（Plan-First）

在执行任何操作之前，先读取 skills/flagos-plugin-fix/SKILL.md 获取完整修复流程指令。

**重要：所有 /flagos-workspace 下的文件操作必须通过 docker exec 在容器内执行。**

## 工作流（本段只执行步骤 3 — Plugin 修复循环）

**前段状态（段1已完成，无需验证）**：
- 步骤 1/2 已在段1完成
- 容器 ${SEG_CTR} 已就绪，env_type=vllm_plugin_flaggems 已确认
- **禁止**回头检查或重做步骤 1/2

### 步骤 3：Plugin 修复（核心）

循环（最多 ${MAX_FIX_ROUNDS} 轮）：
1. 清理缓存: rm -rf /root/.triton/cache/ /tmp/triton_cache/ /root/.flaggems/code_cache/
2. 启动服务: start_service.sh --mode flagos
3. 等待服务: wait_for_service.sh --timeout 180 --max-timeout 600
4. 成功 → fixed，退出循环
5. 失败 → 读崩溃日志 → 分析 traceback → 定位 plugin 源码 → 修改代码 → 下一轮

**修复策略优先级**：
1. ImportError → 修复 import 路径
2. AttributeError → 适配 API 变更
3. 算子注册错误 → 修正注册逻辑或禁用问题算子
4. Triton 编译错误 → 修正 kernel 或禁用算子
5. 其他 RuntimeError → 根据 traceback 修复

**终止条件**：
- 服务启动成功 → final_status=fixed, workflow.service_ok=true
- 达到最大轮次 → final_status=max_rounds_exceeded, workflow.service_ok=false
- 连续 2 轮相同错误无新思路 → final_status=unfixable, workflow.service_ok=false

**约束**：
- 只能修改 plugin 代码，禁止改 vllm/flaggems 核心
- 禁止降级到非 plugin 模式
- 每轮必须有明确变更记录

## 通用规则

${COMMON_PLAN_STEPS}

**进度输出**：
- [步骤3] Plugin 修复 — 第 N/${MAX_FIX_ROUNDS} 轮
- ✓ 修复成功：修改了 xxx.py 第 N 行
- ✗ 第 N 轮仍失败：<错误摘要>

**⚠ 段边界（硬性约束 — 最高优先级）**：
- 本段**只执行步骤3**（Plugin修复循环），修复完成或失败后**必须立即停止**
- **绝对禁止**执行步骤4（精度性能评测）、步骤5（打包发布）
- 完成标志：输出 \"[段2] 步骤3完成，context 已同步\" 后立即停止"

claude -p "${PROMPT_SEG2}" \
    --permission-mode auto \
    --output-format stream-json \
    --verbose \
    --debug-file "${DEBUG_FILE}.seg2" \
    --max-turns 500 \
    2>&1 | tee -a "${LOG_FILE}" \
         | tee >(python3 "${SCRIPT_DIR}/stream_to_debug_log.py" >> "${FULL_LOG}") \
         | python3 "${SCRIPT_DIR}/stream_filter.py" --pipeline-log "${PIPELINE_LOG}" --terminal-log "${TERMINAL_LOG}" --cost-file "${LOG_DIR}/seg2_cost.txt" ${FILTER_FLAGS} || true

# 段2完成 — 段间检查
SEG2_END_TS=$(date +%s)
SEG2_ELAPSED=$(( SEG2_END_TS - SEG2_START_TS ))
SEG2_MIN=$(( SEG2_ELAPSED / 60 ))
SEG2_SEC=$(( SEG2_ELAPSED % 60 ))
echo ""
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│  段2 完成 — 耗时 ${SEG2_MIN}m ${SEG2_SEC}s                                     │"
echo "└──────────────────────────────────────────────────────────────┘"

# 强制同步 context
if [ -f "${SHARED_CTX}" ]; then
    cp "${SHARED_CTX}" "${CTX_FILE}"
elif docker inspect --type=container "${SEG_CTR}" &>/dev/null; then
    docker cp "${SEG_CTR}:/flagos-workspace/shared/context.yaml" "${CTX_FILE}" 2>/dev/null || true
fi

CTX_INFO=$(read_plugin_fix_context "${MODEL}" 2>/dev/null) || { echo "错误：段2未更新 context_snapshot.yaml，终止"; exit 1; }
SEG_FIX_STATUS=$(echo "$CTX_INFO" | cut -d'|' -f3)
SEG_SERVICE_OK=$(echo "$CTX_INFO" | cut -d'|' -f4)

echo "  修复状态: ${SEG_FIX_STATUS}"
echo "  服务可用: ${SEG_SERVICE_OK}"
echo ""

# ╔══════════════════════════════════════════════════════════════╗
# ║  段3/3  精度性能评测 + 打包发布  (步骤 4→5)                    ║
# ╚══════════════════════════════════════════════════════════════╝

SEG3_ELAPSED=0
SEG3_MIN=0
SEG3_SEC=0

if [ "${SEG_SERVICE_OK}" != "True" ] && [ "${SEG_SERVICE_OK}" != "true" ]; then
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  段3 跳过 — Plugin 修复失败 (service_ok=false)               ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""
    echo "  修复状态: ${SEG_FIX_STATUS}"
    echo "  跳过步骤 4（精度性能评测）和步骤 5（打包发布）"
    echo "  诊断报告: /data/plugin-fix-workspace/${MODEL}/results/plugin_fix_diagnosis.md"
else
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  段3/3  精度性能评测 + 打包发布  (步骤 4→5)                    ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    SEG3_START_TS=$(date +%s)
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 段3 开始"

    PROMPT_SEG3="你正在执行 **Plugin Fix Pipeline 段3**，目标是采集精度性能数据并打包发布。

## 基本信息（段1+段2已完成，以下为段间传递的参数）

- 容器名: ${SEG_CTR}
- 模型名: ${MODEL}
- 容器内模型路径: ${SEG_MODEL_PATH}
- GPU: ${SEG_GPU_COUNT} x ${SEG_GPU_TYPE}
- 服务端口: ${SEG_PORT}
- 修复状态: ${SEG_FIX_STATUS}
- 服务可用: ${SEG_SERVICE_OK}
${COMMON_TOKENS}

**变量定义**：CONTAINER=${SEG_CTR}

**重要：所有 /flagos-workspace 下的文件操作必须通过 docker exec 在容器内执行。**

## 工作流（本段执行步骤 4+5）

**前段状态（段1+段2已完成，无需验证）**：
- 步骤 1/2/3 已在前两段完成
- 容器 ${SEG_CTR} 已就绪，plugin 已修复，服务可正常启动
- **禁止**回头检查或重做步骤 1/2/3

### 步骤 4：精度性能评测

前置：workflow.service_ok=true（已由 shell 层验证）。

1. 确保服务运行中（如未运行则启动：start_service.sh --mode flagos）
2. 精度评测（GPQA Diamond）：
   docker exec \${CONTAINER} bash -c \"PATH=/opt/conda/bin:\\\$PATH python3 /flagos-workspace/scripts/fast_gpqa.py \\
     --port \$PORT --model-name '\$MODEL_NAME' --output-dir /flagos-workspace/results/ --output-name plugin_fix_accuracy --json\"
3. 停止服务，docker restart \${CONTAINER}，等待 5 秒
4. 重新启动服务
5. 性能评测（4k1k quick）：
   docker exec \${CONTAINER} bash -c \"PATH=/opt/conda/bin:\\\$PATH python3 /flagos-workspace/scripts/benchmark_runner.py \\
     --port \$PORT --model-name '\$MODEL_NAME' --mode quick --output-dir /flagos-workspace/results/ --output-name plugin_fix_performance --json\"
6. 记录结果到 context.yaml（仅记录，不做达标判定）
7. 停止服务

### 步骤 5：打包发布

1. 停止容器内所有推理服务
2. Docker commit：
   docker commit \${CONTAINER} harbor.baai.ac.cn/flagrelease-public/<MODEL_REPO>:<DATE_TAG>-plugin-fix
3. Docker push 到 Harbor：
   docker login harbor.baai.ac.cn -u \${HARBOR_USER} -p \${HARBOR_PASSWORD}
   docker push harbor.baai.ac.cn/flagrelease-public/<MODEL_REPO>:<DATE_TAG>-plugin-fix
4. 写入 release 信息到 context.yaml
5. 生成最终报告

## 通用规则

${COMMON_PLAN_STEPS}

**进度输出**：
- [步骤4] 精度性能评测 — 开始 / 完成 (耗时)
- [步骤5] 打包发布 — 开始 / 完成 (耗时)

**⚠ 段边界**：
- 本段执行步骤4（精度性能评测）+ 步骤5（打包发布），完成后停止。
- 完成标志：输出 \"[段3] 步骤4/5全部完成\" 后停止"

    claude -p "${PROMPT_SEG3}" \
        --permission-mode auto \
        --output-format stream-json \
        --verbose \
        --debug-file "${DEBUG_FILE}.seg3" \
        --max-turns 200 \
        2>&1 | tee -a "${LOG_FILE}" \
             | tee >(python3 "${SCRIPT_DIR}/stream_to_debug_log.py" >> "${FULL_LOG}") \
             | python3 "${SCRIPT_DIR}/stream_filter.py" --pipeline-log "${PIPELINE_LOG}" --terminal-log "${TERMINAL_LOG}" --cost-file "${LOG_DIR}/seg3_cost.txt" ${FILTER_FLAGS} || true

    SEG3_END_TS=$(date +%s)
    SEG3_ELAPSED=$(( SEG3_END_TS - SEG3_START_TS ))
    SEG3_MIN=$(( SEG3_ELAPSED / 60 ))
    SEG3_SEC=$(( SEG3_ELAPSED % 60 ))
    echo ""
    echo "┌──────────────────────────────────────────────────────────────┐"
    echo "│  段3 完成 — 耗时 ${SEG3_MIN}m ${SEG3_SEC}s                                     │"
    echo "└──────────────────────────────────────────────────────────────┘"
fi

# ========== 流程结束 ==========
PIPELINE_END_TS=$(date +%s)
PIPELINE_ELAPSED=$(( PIPELINE_END_TS - PIPELINE_START_TS ))
PIPELINE_MIN=$(( PIPELINE_ELAPSED / 60 ))
PIPELINE_SEC=$(( PIPELINE_ELAPSED % 60 ))

# 读取各段费用
SEG1_COST=$(cat "${LOG_DIR}/seg1_cost.txt" 2>/dev/null || echo "N/A")
SEG2_COST=$(cat "${LOG_DIR}/seg2_cost.txt" 2>/dev/null || echo "N/A")
SEG3_COST=$(cat "${LOG_DIR}/seg3_cost.txt" 2>/dev/null || echo "N/A")
TOTAL_COST=$(SEG1_COST="${SEG1_COST}" SEG2_COST="${SEG2_COST}" SEG3_COST="${SEG3_COST}" python3 -c "
import os
costs = []
for k in ['SEG1_COST', 'SEG2_COST', 'SEG3_COST']:
    try: costs.append(float(os.environ.get(k, '').strip()))
    except: pass
print(f'{sum(costs):.2f}' if costs else 'N/A')
" 2>/dev/null || echo "N/A")

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Plugin Fix Pipeline 完成 — 耗时 & 费用汇总                  ║"
echo "╠══════════════════════════════════════════════════════════════╣"
printf "║  段1  容器准备+环境检测         %6s   \$%-8s║\n" "${SEG1_MIN}m${SEG1_SEC}s" "${SEG1_COST}"
printf "║  段2  Plugin修复循环            %6s   \$%-8s║\n" "${SEG2_MIN}m${SEG2_SEC}s" "${SEG2_COST}"
printf "║  段3  评测+发布                 %6s   \$%-8s║\n" "${SEG3_MIN}m${SEG3_SEC}s" "${SEG3_COST}"
echo "║──────────────────────────────────────────────────────────────║"
printf "║  总计                           %6s   \$%-8s║\n" "${PIPELINE_MIN}m${PIPELINE_SEC}s" "${TOTAL_COST}"
echo "╚══════════════════════════════════════════════════════════════╝"

# 同步最终 context
CTX_FILE="/data/plugin-fix-workspace/${MODEL}/config/context_snapshot.yaml"
if [ -f "${CTX_FILE}" ]; then
    cp "${CTX_FILE}" "/data/plugin-fix-workspace/${MODEL}/config/context_final.yaml"
    echo "  ✓ 最终 context 已保存"
fi

# 输出结果摘要
if [ -f "${CTX_FILE}" ]; then
    python3 -c "
import yaml
with open('${CTX_FILE}') as f:
    ctx = yaml.safe_load(f)
fix = ctx.get('plugin_fix', {})
wf = ctx.get('workflow', {})
rel = ctx.get('release', {})
print(f'  修复状态: {fix.get(\"final_status\", \"unknown\")}')
print(f'  修复轮次: {fix.get(\"current_round\", 0)}/{fix.get(\"max_rounds\", 5)}')
print(f'  服务可用: {wf.get(\"service_ok\", False)}')
if rel.get('pushed'):
    print(f'  Harbor 镜像: {rel.get(\"harbor_image\", \"\")}:{rel.get(\"harbor_tag\", \"\")}')
" 2>/dev/null || true
fi

echo ""
echo "完整报告: /data/plugin-fix-workspace/${MODEL}/results/report.md"
