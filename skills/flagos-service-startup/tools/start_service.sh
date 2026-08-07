#!/bin/bash
# start_service.sh — 从 context.yaml 读取配置并启动 sglang 服务（sglang 分支）
#
# 供 operator_search.py 的 --service-startup-cmd 调用。
# 在容器内执行，读取 /flagos-workspace/shared/context.yaml 获取启动参数。
#
# 用法:
#   bash /flagos-workspace/scripts/start_service.sh
#   bash /flagos-workspace/scripts/start_service.sh --mode flagos
#   bash /flagos-workspace/scripts/start_service.sh --mode native

set -euo pipefail

# Source DTK env (set +eu needed: env.sh uses unbound vars)
set +eu; [ -f /opt/dtk/env.sh ] && source /opt/dtk/env.sh 2>/dev/null; set -eu

CONTEXT_YAML="/flagos-workspace/shared/context.yaml"
MODE=""
# Python 二进制前缀（sglang 分支非 conda；可被 PYTHON_BIN_DIR 环境变量
# 或 context runtime.python_bin_dir 覆盖）
PY_BIN_DIR="${PYTHON_BIN_DIR:-/usr/local/python3.11.14/bin}"
PYTHON="${PY_BIN_DIR}/python3"
# SGLANG_PLUGINS 覆盖：未设=沿用旧自动行为；显式设置（含空串）=强制覆盖
# 取值: "" | "sglang_fl" | 逗号分隔多值
SGLANG_PLUGINS_OVERRIDE_SET=0
SGLANG_PLUGINS_OVERRIDE=""
# 显式指定日志文件（调用方需要监控与写入落到同一文件时使用，如 baseline_selector 三选各 variant 独立日志）
# 不传时回退到默认 startup_${MODE}.log，保持所有现存调用行为不变。
LOG_FILE_OVERRIDE=""

# 解析参数（支持 --mode flagos / --mode=flagos / 裸值）
while [[ $# -gt 0 ]]; do
    case $1 in
        --mode=*) MODE="${1#--mode=}"; shift ;;
        --mode)   MODE="${2:-}"; shift; shift 2>/dev/null || true ;;
        --sglang-plugins=*) SGLANG_PLUGINS_OVERRIDE="${1#--sglang-plugins=}"; SGLANG_PLUGINS_OVERRIDE_SET=1; shift ;;
        --sglang-plugins)   SGLANG_PLUGINS_OVERRIDE="${2:-}"; SGLANG_PLUGINS_OVERRIDE_SET=1; shift; shift 2>/dev/null || true ;;
        --log-file=*) LOG_FILE_OVERRIDE="${1#--log-file=}"; shift ;;
        --log-file)   LOG_FILE_OVERRIDE="${2:-}"; shift; shift 2>/dev/null || true ;;
        *)        shift ;;
    esac
done

# 如果未传 --mode，从环境变量 USE_FLAGGEMS 推断
if [ -z "$MODE" ]; then
    if [ "${USE_FLAGGEMS:-}" = "0" ]; then
        MODE="native"
    elif [ "${USE_FLAGGEMS:-}" = "1" ]; then
        MODE="flagos"
    else
        MODE="flagos"
    fi
    echo "[start_service.sh] --mode 未指定，从环境推断 mode=${MODE}"
fi

# flagos_optimized 也是 FlagGems 启用模式
case "$MODE" in
    native)       USE_FLAGGEMS_FLAG=0 ;;
    flagos|flagos_optimized|flagos_full)  USE_FLAGGEMS_FLAG=1 ;;
    *)            USE_FLAGGEMS_FLAG=1 ;;
esac

# 从 context.yaml 读取启动参数
read_context() {
    "${PYTHON}" -c "
import yaml, json, sys
with open('${CONTEXT_YAML}') as f:
    ctx = yaml.safe_load(f)

model_path = ctx.get('model', {}).get('container_path', '')
model_name = ctx.get('model', {}).get('name', '').split('/')[-1]
port = ctx.get('service', {}).get('port', 8000)
tp_size = ctx.get('runtime', {}).get('tp_size', 0)
gpu_count = ctx.get('runtime', {}).get('gpu_count', ctx.get('gpu', {}).get('count', 0))
max_model_len = ctx.get('service', {}).get('max_model_len', 32768)
framework = ctx.get('runtime', {}).get('framework') or 'sglang'  # 仅支持 sglang；空值/缺失均兜底为 sglang
cuda_visible = ctx.get('runtime', {}).get('cuda_visible_devices', '')
visible_devices_env = ctx.get('gpu', {}).get('visible_devices_env', 'CUDA_VISIBLE_DEVICES')
thinking = ctx.get('runtime', {}).get('thinking_model', False)
python_bin_dir = ctx.get('runtime', {}).get('python_bin_dir', '')
engine_flags = ctx.get('runtime', {}).get('engine_flags', '')
if isinstance(engine_flags, list):
    engine_flags = ' '.join(str(f) for f in engine_flags)

# TP fallback: 如果为 0，使用 GPU 数量
if tp_size <= 0:
    tp_size = gpu_count if gpu_count > 0 else 1

print(json.dumps({
    'model_path': model_path,
    'model_name': model_name,
    'port': port,
    'tp_size': tp_size,
    'max_model_len': max_model_len,
    'framework': framework,
    'cuda_visible': cuda_visible,
    'visible_devices_env': visible_devices_env,
    'thinking': thinking,
    'python_bin_dir': python_bin_dir,
    'engine_flags': engine_flags,
}))
"
}

CONFIG_JSON=$(read_context)

MODEL_PATH=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['model_path'])")
MODEL_NAME=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['model_name'])")
PORT=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['port'])")
TP_SIZE=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tp_size'])")
MAX_MODEL_LEN=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['max_model_len'])")
FRAMEWORK=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['framework'])")
CUDA_VISIBLE=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['cuda_visible'])")
VISIBLE_ENV=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['visible_devices_env'])")
THINKING=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['thinking'])")
ENGINE_FLAGS=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['engine_flags'])" 2>/dev/null || true)
CTX_PY_BIN=$(echo "$CONFIG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['python_bin_dir'])" 2>/dev/null || true)

# context 可覆盖 python 前缀（runtime.python_bin_dir）
if [ -n "$CTX_PY_BIN" ]; then
    PY_BIN_DIR="$CTX_PY_BIN"
    PYTHON="${PY_BIN_DIR}/python3"
fi

if [ -z "$MODEL_PATH" ]; then
    echo "ERROR: model.container_path 为空，无法启动服务" >&2
    exit 1
fi

# 强制清理残留进程和编译缓存（每次启动前无条件执行）
pkill -9 -f 'sglang.launch_server|sglang serve|python3 -m sglang' 2>/dev/null || true
for _i in $(seq 1 15); do
    if ! ss -tlnp 2>/dev/null | grep -qE ":${PORT}\b"; then break; fi
    sleep 1
done
rm -rf /root/.triton/cache/ /tmp/triton_cache/ /root/.flaggems/code_cache/ 2>/dev/null || true
echo "[start_service.sh] 已清理残留进程和编译缓存"

# 补丁保护第二防线：启动前检查 ascend 兼容补丁（triton/flag_gems），缺失自动重打
if [ -f /flagos-workspace/scripts/apply_patches.sh ]; then
    if bash /flagos-workspace/scripts/apply_patches.sh --verify >/dev/null 2>&1; then
        echo "[start_service.sh] ascend 补丁检查通过"
    else
        echo "[start_service.sh] ascend 补丁缺失，自动重打..."
        bash /flagos-workspace/scripts/apply_patches.sh --apply || \
            echo "[start_service.sh] WARNING: 补丁重打失败，FlagGems 可能崩溃（gelu_tanh KeyError）" >&2
    fi
fi

# 端口占用检测与自动递增（最多尝试 +10）
ORIGINAL_PORT="$PORT"
for i in $(seq 0 10); do
    CANDIDATE_PORT=$((ORIGINAL_PORT + i))
    if ! ss -tlnp 2>/dev/null | grep -qE ":${CANDIDATE_PORT}\b" && \
       ! netstat -tlnp 2>/dev/null | grep -qE ":${CANDIDATE_PORT}\b"; then
        PORT="$CANDIDATE_PORT"
        break
    fi
    if [ $i -eq 10 ]; then
        echo "ERROR: 端口 ${ORIGINAL_PORT}-${CANDIDATE_PORT} 全部被占用" >&2
        exit 1
    fi
done
if [ "$PORT" != "$ORIGINAL_PORT" ]; then
    echo "[start_service.sh] 端口 ${ORIGINAL_PORT} 被占用，自动递增到 ${PORT}"
fi

# 设置 GPU 可见设备（根据厂商使用对应环境变量名）
if [ -n "$CUDA_VISIBLE" ]; then
    if [[ "$VISIBLE_ENV" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        export "${VISIBLE_ENV}=${CUDA_VISIBLE}"
    else
        echo "WARNING: VISIBLE_ENV='${VISIBLE_ENV}' 不是合法变量名，使用 CUDA_VISIBLE_DEVICES" >&2
        export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE}"
    fi
fi

# 确保 sglang 环境在 PATH 中（sglang 分支：非 conda，python 前缀默认 /usr/local/python3.11.14/bin）
export PATH="${PY_BIN_DIR}:${PATH}"

# 加载持久化的 FlagGems 相关环境变量（只提取相关变量，避免覆盖 PATH 等系统变量）
if [ -f /etc/environment ]; then
    while IFS='=' read -r key val; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        case "$key" in
            USE_FLAGGEMS|FLAGGEMS_*|SGLANG_FLAGGEMS_*|SGLANG_FL_*|SGLANG_PLUGINS)
                val="${val%\"}" ; val="${val#\"}"
                val="${val%\'}" ; val="${val#\'}"
                export "$key=$val"
                ;;
        esac
    done < /etc/environment
fi

# sglang 分支：无代码注入控制文件（FLAGGEMS_CONTROL_MODE 机制不适用），
# 算子控制统一走 SGLANG_FL_* 环境变量（黑/白名单）。

# 根据 mode 强制覆盖 USE_FLAGGEMS（确保 native/flagos 模式正确）
export USE_FLAGGEMS="$USE_FLAGGEMS_FLAG"

# native 模式下关闭插件两层替换（Layer1 总开关 + Layer2 fused kernels）并清残留配置
if [ "$MODE" = "native" ]; then
    export SGLANG_FL_OOT_ENABLED=0
    unset SGLANG_FL_PREFER SGLANG_FL_PER_OP 2>/dev/null || true
    unset SGLANG_FL_FLAGOS_BLACKLIST SGLANG_FL_FLAGOS_WHITELIST 2>/dev/null || true
    unset SGLANG_FL_OOT_BLACKLIST SGLANG_FL_OOT_WHITELIST 2>/dev/null || true
fi

# SGLANG_PLUGINS 决策（优先级从高到低）：
#   1. 显式 --sglang-plugins（含空串）→ 强制覆盖
#   2. 持久化/继承值（含空串）→ 沿用（baseline_selector.py / persist_op_config.py 固化）
#   3. 均未设置 → 自动兜底（USE_FLAGGEMS=1 且 sglang_fl 存在 → sglang_fl）
#   sglang_fl 插件本身经 entry_points 自动发现；显式指定 SGLANG_PLUGINS 用于
#   多插件过滤与 baseline 纯净场景（配合 USE_FLAGGEMS=0 + SGLANG_FL_OOT_ENABLED=0）
if [ "$SGLANG_PLUGINS_OVERRIDE_SET" = "1" ]; then
    export SGLANG_PLUGINS="$SGLANG_PLUGINS_OVERRIDE"
    if [ -z "$SGLANG_PLUGINS_OVERRIDE" ]; then
        echo "[start_service.sh] 显式覆盖：SGLANG_PLUGINS=（空）"
    else
        echo "[start_service.sh] 显式覆盖：SGLANG_PLUGINS=${SGLANG_PLUGINS_OVERRIDE}"
    fi
elif [ -n "${SGLANG_PLUGINS+x}" ]; then
    export SGLANG_PLUGINS
    echo "[start_service.sh] 继承持久化 plugin 配置：SGLANG_PLUGINS='${SGLANG_PLUGINS}'"
elif [ "$USE_FLAGGEMS_FLAG" = "1" ]; then
    HAS_PLUGIN=$("${PYTHON}" -c "
import importlib.util
print('yes' if importlib.util.find_spec('sglang_fl') else 'no')
" 2>/dev/null || echo "no")
    if [ "$HAS_PLUGIN" = "yes" ]; then
        export SGLANG_PLUGINS="sglang_fl"
        echo "[start_service.sh] plugin 场景：设置 SGLANG_PLUGINS=sglang_fl"
    fi
fi

# 日志文件：优先用 --log-file 显式指定，否则回退默认 startup_${MODE}.log（保持现存调用不变）
LOG_FILE="${LOG_FILE_OVERRIDE:-/flagos-workspace/logs/startup_${MODE}.log}"

# 创建 startup_default.log 符号链接指向当前实际日志
# 崩溃诊断脚本统一引用 startup_default.log，确保路径一致；软链须跟随 LOG_FILE（含 --log-file 覆盖）
if [ "$MODE" != "default" ]; then
    ln -sf "$(basename "$LOG_FILE")" /flagos-workspace/logs/startup_default.log
fi

# FlagGems 模式启动前清理 Triton/FlagGems 编译缓存（约束39：避免旧缓存隐藏问题算子）
if [ "$USE_FLAGGEMS_FLAG" = "1" ]; then
    rm -rf /root/.triton/cache/ /tmp/triton_cache/ /root/.flaggems/code_cache/ 2>/dev/null || true
fi

# 构建启动命令（sglang 分支：仅支持 sglang）
if [ "$FRAMEWORK" != "sglang" ]; then
    echo "ERROR: 仅支持 sglang 框架，但 runtime.framework='${FRAMEWORK}'。请检查 context.yaml。" >&2
    exit 1
fi
CMD="sglang serve --model-path '${MODEL_PATH}' \
    --host 0.0.0.0 \
    --port ${PORT} \
    --served-model-name '${MODEL_NAME}' \
    --tp-size ${TP_SIZE} \
    --context-length ${MAX_MODEL_LEN} \
    --trust-remote-code"

# sglang 特有 flags（context runtime.engine_flags，逐词追加；
# 如 --disable-radix-cache --page-size 16 --mem-fraction-static 0.7）
if [ -n "$ENGINE_FLAGS" ]; then
    CMD="$CMD ${ENGINE_FLAGS}"
fi

# Thinking model 添加 reasoning parser（sglang 用连字符命名）
if [ "$THINKING" = "true" ]; then
    # 根据模型名推断 parser
    MODEL_LOWER=$(echo "$MODEL_NAME" | tr '[:upper:]' '[:lower:]')
    if echo "$MODEL_LOWER" | grep -qE 'qwen3|qwq'; then
        CMD="$CMD --reasoning-parser qwen3"
    elif echo "$MODEL_LOWER" | grep -qE 'deepseek'; then
        CMD="$CMD --reasoning-parser deepseek-r1"
    fi
fi

echo "[start_service.sh] mode=${MODE}, framework=${FRAMEWORK}, port=${PORT}, tp=${TP_SIZE}"
echo "[start_service.sh] CMD: ${CMD}"

# 后台启动，日志写入文件
nohup bash -c "cd /flagos-workspace && ${CMD}" > "${LOG_FILE}" 2>&1 &
SVC_PID=$!
echo "${SVC_PID}" > /flagos-workspace/logs/service.pid
echo "${LOG_FILE}" > /flagos-workspace/logs/service_log_path
# 回写服务实际监听端口：PORT 可能因端口占用被自动递增（见上方递增逻辑），
# 下游冒烟/评测必须读此文件而非假设 8000，否则会连错端口或连到别的服务导致误判。
echo "${PORT}" > /flagos-workspace/logs/service_port
echo "[start_service.sh] PID=${SVC_PID}, log=${LOG_FILE}, port=${PORT}"

# sglang 分支：无控制文件机制；算子配置来自 /etc/environment 持久化 env，
# 配置 vs 运行时对比以启动日志中的 env 快照与 dispatch 记录为准。

# 短暂等待后验证进程是否存活（快速发现启动参数错误导致的立即崩溃）
sleep 2
if ! kill -0 "${SVC_PID}" 2>/dev/null; then
    echo "ERROR: 服务进程 ${SVC_PID} 启动后立即退出，请检查日志: ${LOG_FILE}" >&2
    tail -20 "${LOG_FILE}" 2>/dev/null >&2
    exit 1
fi
