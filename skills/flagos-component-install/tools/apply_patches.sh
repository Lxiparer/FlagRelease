#!/bin/bash
# apply_patches.sh — FlagGems/triton-cx ascend 兼容补丁保护（sglang 分支专用）
#
# 容器内手工补丁（triton libdevice pow constexpr / flag_gems pow dtype /
# CUSTOMIZED_UNUSED_OPS / enable() record 默认）会被 pip install/upgrade
# 冲掉 → gelu_tanh KeyError → 服务崩溃。本脚本以文件末尾 marker 注释为
# 锚点幂等重打，供 install_component.py（安装后自动 --apply）与
# start_service.sh（启动前 --verify，缺失则 --apply）两个防线调用。
#
# 用法（容器内）:
#   bash apply_patches.sh --apply    # 幂等重打：已应用(marker 存在)跳过
#   bash apply_patches.sh --verify   # 检查全部 marker；exit 0=就位 1=缺失
#   bash apply_patches.sh --force    # 无条件重打（marker 移除后重新 patch）
#   bash apply_patches.sh --status   # 逐条打印状态（默认）
#
# 环境变量: PYTHON_BIN_DIR 覆盖 python 前缀（默认 /usr/local/python3.11.14/bin）

set -euo pipefail

PYTHON_BIN_DIR="${PYTHON_BIN_DIR:-/usr/local/python3.11.14/bin}"
SITE_PACKAGES="$("${PYTHON_BIN_DIR}/python3" -c 'import site; print(site.getsitepackages()[0])')"
PATCHES_DIR="$(cd "$(dirname "$(readlink -f "$0")")/../patches" && pwd)"

# 补丁定义（| 分隔）：目标文件(相对 site-packages) | marker 锚点 | patch 文件名
PATCHES=(
    "triton/language/extra/cann/libdevice.py|flagos-patch-1-triton-libdevice-pow|patch_1_triton_libdevice_pow.patch"
    "flag_gems/runtime/backend/_ascend/ops/pow.py|flagos-patch-2-flag-gems-pow-dtype|patch_2_flag_gems_pow_dtype.patch"
    "flag_gems/runtime/backend/_ascend/__init__.py|flagos-patch-3-customized-unused-ops|patch_3_flag_gems_customized_unused_ops.patch"
    "flag_gems/__init__.py|flagos-patch-4-flag-gems-record-default|patch_4_flag_gems_record_default.patch"
)

patch_file_of() { echo "$1" | cut -d'|' -f3; }
marker_of()     { echo "$1" | cut -d'|' -f2; }
target_of()     { echo "$SITE_PACKAGES/$(echo "$1" | cut -d'|' -f1)"; }

is_applied() {
    local target marker
    target="$(target_of "$1")"
    marker="$(marker_of "$1")"
    [ -f "$target" ] && grep -qF "$marker" "$target"
}

apply_one() {
    local spec="$1" target marker patchfile
    target="$(target_of "$spec")"
    marker="$(marker_of "$spec")"
    patchfile="$PATCHES_DIR/$(patch_file_of "$spec")"

    if is_applied "$spec"; then
        echo "[apply_patches] SKIP  ${target}（marker 已存在）"
        return 0
    fi
    if [ ! -f "$patchfile" ]; then
        echo "[apply_patches] ERROR ${target}: patch 文件缺失 ${patchfile}" >&2
        return 1
    fi
    if [ ! -f "$target" ]; then
        echo "[apply_patches] ERROR ${target}: 目标文件不存在（组件未安装？先 install_component）" >&2
        return 1
    fi

    # 用 --forward 幂等：代码特征已在但 marker 缺失（异常态）时不会重复打
    if (cd "$SITE_PACKAGES" && patch -p1 --forward --batch < "$patchfile" > /tmp/apply_patches.$$.log 2>&1); then
        :
    elif grep -qE "Reversed|already applied" /tmp/apply_patches.$$.log; then
        echo "[apply_patches] NOTE  ${target}: 代码已含补丁（上次应用未写 marker），仅补 marker"
    else
        echo "[apply_patches] ERROR ${target}: patch 应用失败，日志见 /tmp/apply_patches.$$.log" >&2
        cat /tmp/apply_patches.$$.log >&2
        return 1
    fi
    echo "# ${marker}: applied by apply_patches.sh $(date -u +%Y%m%dT%H%M%SZ)" >> "$target"
    echo "[apply_patches] APPLY ${target} ✓"
}

verify_all() {
    local missing=0
    for spec in "${PATCHES[@]}"; do
        if is_applied "$spec"; then
            echo "[apply_patches] OK    $(target_of "$spec")"
        else
            echo "[apply_patches] MISS  $(target_of "$spec")"
            missing=1
        fi
    done
    return "$missing"
}

ACTION="${1:---status}"
case "$ACTION" in
    --apply)
        ok=0
        for spec in "${PATCHES[@]}"; do apply_one "$spec" || ok=1; done
        [ "$ok" -eq 0 ] || { echo "[apply_patches] 存在失败项" >&2; exit 1; }
        echo "[apply_patches] 全部补丁就位"
        ;;
    --verify)
        verify_all
        ;;
    --force)
        for spec in "${PATCHES[@]}"; do
            sed -i "/$(marker_of "$spec" | sed 's/[.[\*^$(){}?+|]/\\&/g')/d" "$(target_of "$spec")" 2>/dev/null || true
        done
        ok=0
        for spec in "${PATCHES[@]}"; do apply_one "$spec" || ok=1; done
        [ "$ok" -eq 0 ] || { echo "[apply_patches] 存在失败项" >&2; exit 1; }
        echo "[apply_patches] --force 完成"
        ;;
    --status)
        verify_all || true
        ;;
    *)
        echo "用法: $0 [--apply|--verify|--force|--status]" >&2
        exit 2
        ;;
esac
