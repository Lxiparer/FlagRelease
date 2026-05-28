#!/bin/bash
# Plugin Fix Pipeline — 一键启动脚本
# 修复已有 plugin 环境的镜像，使其能正常启动推理服务，采集数据并打包发布。
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
    echo ""
    echo "示例:"
    echo "  $0 harbor.baai.ac.cn/flagrelease/qwen3-plugin:latest Qwen3-8B harbor_user harbor_pass"
    echo "  $0 harbor.baai.ac.cn/flagrelease/qwen3-plugin:latest Qwen3-8B harbor_user harbor_pass --proxy http://proxy:80 --max-fix-rounds 3"
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
echo "  Plugin Fix Pipeline"
echo "============================================================"
echo "  镜像: ${IMAGE}"
echo "  模型: ${MODEL}"
echo "  模型路径: ${MODEL_PATH}"
echo "  最大修复轮次: ${MAX_FIX_ROUNDS}"
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

# ========== 构造 Prompt ==========
COMMON_TOKENS="
**凭证**（已通过 setup_workspace.sh 写入容器 /flagos-workspace/.env）：
  HARBOR_USER=${HARBOR_USER}
  HARBOR_PASSWORD=${HARBOR_PASSWORD}

**网络代理**：
  FLAGOS_PROXY_LIST=${FLAGOS_PROXY_LIST:-}
  FLAGOS_ACTIVE_PROXY=${http_proxy:-}
  所有需要外网的 docker exec 命令必须传入代理: docker exec -e http_proxy=${http_proxy:-} -e https_proxy=${https_proxy:-} ...
"

PROMPT=$(cat <<PROMPT_EOF
你正在执行 **Plugin Fix Pipeline**，目标是修复一个 plugin 环境的 Docker 镜像使其能正常启动推理服务，然后采集精度性能数据并打包发布。

## 基本信息

- 镜像: ${IMAGE}
- 模型名: ${MODEL}
- 宿主机模型路径: ${MODEL_PATH}
- 容器内模型路径: ${CONTAINER_MODEL_PATH}
- 最大修复轮次: ${MAX_FIX_ROUNDS}
${COMMON_TOKENS}

## 执行模式：计划优先（Plan-First）

在执行任何操作之前，先读取以下 SKILL.md 提取关键命令和参数：
- skills/flagos-container-preparation/SKILL.md（步骤1容器准备）
- skills/flagos-pre-service-inspection/SKILL.md（步骤2环境检测）
- skills/flagos-plugin-fix/SKILL.md（步骤3修复流程）

**重要：所有 /flagos-workspace 下的文件操作必须通过 docker exec 在容器内执行。**

## 工作流（5步，全自动，零交互）

### 步骤 1：容器准备

1. 从镜像 ${IMAGE} 创建容器（GPU 自动检测，参照 SKILL.md 中 GPU 厂商对应模板）
2. 搜索模型权重：python3 skills/flagos-container-preparation/tools/check_model_local.py --model "${MODEL}" --mode container --container \$CONTAINER --output-json
3. 部署工具脚本：bash skills/flagos-container-preparation/tools/setup_workspace.sh \$CONTAINER ${MODEL} --context-template plugin_fix
4. 写入 context.yaml + traces/01_container_preparation.json

### 步骤 2：环境检测

1. docker exec \$CONTAINER bash -c "PATH=/opt/conda/bin:\\\$PATH python3 /flagos-workspace/scripts/inspect_env.py --json"
2. 确认 env_type=vllm_plugin_flaggems（如果不是，终止并报错）
3. 确认 plugin 已安装且可 import
4. 记录 plugin 源码路径
5. 写入 traces/02_environment_inspection.json

### 步骤 3：Plugin 修复（核心）

**读取 skills/flagos-plugin-fix/SKILL.md 获取完整修复指令。**

循环（最多 ${MAX_FIX_ROUNDS} 轮）：
1. 清理缓存: rm -rf /root/.triton/cache/ /tmp/triton_cache/ /root/.flaggems/code_cache/
2. 启动服务: start_service.sh --mode flagos
3. 等待服务: wait_for_service.sh --timeout 180 --max-timeout 600
4. 成功 → fixed，退出循环
5. 失败 → 读崩溃日志 → 分析 traceback → 定位 plugin 源码 → 修改代码 → 下一轮

**约束**：只能修改 plugin 代码，禁止改 vllm/flaggems 核心。禁止降级到非 plugin 模式。

修复失败 → 跳过步骤 4/5，输出诊断报告。

### 步骤 4：精度性能评测

前置：workflow.service_ok=true，否则跳过。

1. 精度（GPQA Diamond）：fast_gpqa.py --output-name plugin_fix_accuracy
2. 停止服务 → docker restart → 重启服务
3. 性能（4k1k quick）：benchmark_runner.py --mode quick --output-name plugin_fix_performance
4. 仅记录数据，不做达标判定
5. 停止服务

### 步骤 5：打包发布

1. 停止容器内推理服务
2. docker commit \$CONTAINER harbor.baai.ac.cn/flagrelease-public/<MODEL_REPO>:<DATE_TAG>-plugin-fix
3. docker login + docker push
4. 写入 release 信息
5. 生成最终报告

## 通用规则

- context.yaml 更新必须使用 update_context.py（禁止手写 Python 操作 yaml）
- 每个步骤完成后写入 trace JSON + 更新台账 + 同步 context_snapshot.yaml + 生成报告
- 同步命令：docker cp \$CONTAINER:/flagos-workspace/shared/context.yaml /data/flagos-workspace/${MODEL}/config/context_snapshot.yaml
- 报告生成：docker exec \$CONTAINER bash -c "PATH=/opt/conda/bin:\\\$PATH python3 /flagos-workspace/scripts/generate_report.py --output /flagos-workspace/results/report.md"
- 服务等待：wait_for_service.sh 前台阻塞执行，Bash timeout=600000
- 容器内 Python 必须加 PATH=/opt/conda/bin:\$PATH

**进度输出**：
- [步骤X] <名称> — 开始 / 完成 (耗时)
- [步骤3] Plugin 修复 — 第 N/${MAX_FIX_ROUNDS} 轮
- ✓ / ✗ 结果摘要
PROMPT_EOF
)

# ========== 部署权限白名单 ==========
mkdir -p .claude && cp settings.local.json .claude/settings.local.json

# 动态注入模型特定权限
python3 -c "
import json, sys
model = sys.argv[1]
with open('.claude/settings.local.json') as f:
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
with open('.claude/settings.local.json', 'w') as f:
    json.dump(cfg, f, indent=2)
" "${MODEL}"
echo "  ✓ 已注入 ${MODEL} 模型特定权限规则"

# ========== 宿主机历史数据归档 ==========
HOST_BASE="/data/flagos-workspace/${MODEL}"
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
    mkdir -p "/data/flagos-workspace/${MODEL}/${d}"
done

# ========== 启动 Claude Code ==========
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_DIR="/data/flagos-workspace/${MODEL}/logs"
LOG_FILE="${LOG_DIR}/claude_plugin_fix_${TIMESTAMP}.log"
FULL_LOG="${LOG_DIR}/claude_plugin_fix_full_${TIMESTAMP}.log"
DEBUG_FILE="${LOG_DIR}/claude_plugin_fix_debug_${TIMESTAMP}.log"
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

# GPU 服务清理（退出时自动执行）
cleanup_gpu_services() {
    local ctr=""
    local ctx_file="/data/flagos-workspace/${MODEL}/config/context_snapshot.yaml"
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

# 全流程计时
PIPELINE_START_TS=$(date +%s)

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Plugin Fix Pipeline — 全流程单段执行                       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 Claude Code..."
echo ""

claude -p "${PROMPT}" \
    --permission-mode auto \
    --output-format stream-json \
    --verbose \
    --debug-file "${DEBUG_FILE}" \
    --max-turns 500 \
    2>&1 | tee "${LOG_FILE}" \
         | tee >(python3 "${SCRIPT_DIR}/stream_to_debug_log.py" > "${FULL_LOG}") \
         | python3 "${SCRIPT_DIR}/stream_filter.py" --pipeline-log "${PIPELINE_LOG}" --terminal-log "${TERMINAL_LOG}" --cost-file "${LOG_DIR}/cost.txt" ${FILTER_FLAGS} || true

# ========== 流程结束 ==========
PIPELINE_END_TS=$(date +%s)
PIPELINE_ELAPSED=$(( PIPELINE_END_TS - PIPELINE_START_TS ))
PIPELINE_MIN=$(( PIPELINE_ELAPSED / 60 ))
PIPELINE_SEC=$(( PIPELINE_ELAPSED % 60 ))

echo ""
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│  Plugin Fix Pipeline 完成 — 总耗时 ${PIPELINE_MIN}m ${PIPELINE_SEC}s"
echo "└──────────────────────────────────────────────────────────────┘"

# 同步最终 context
CTX_FILE="/data/flagos-workspace/${MODEL}/config/context_snapshot.yaml"
if [ -f "${CTX_FILE}" ]; then
    cp "${CTX_FILE}" "/data/flagos-workspace/${MODEL}/config/context_final.yaml"
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
echo "完整报告: /data/flagos-workspace/${MODEL}/results/report.md"
