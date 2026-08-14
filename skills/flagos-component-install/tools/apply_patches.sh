#!/bin/bash
# apply_patches.sh — FlagGems/triton ascend 兼容补丁保护（sglang 分支专用）
#
# 容器内手工补丁（triton libdevice pow constexpr / flag_gems pow dtype /
# CUSTOMIZED_UNUSED_OPS / enable() record 默认）会被 pip install/upgrade 冲掉
# → gelu_tanh KeyError → 服务崩溃。flagtree 栈另有四个 torch_npu 兼容补丁
# （patch 5-7 + extra/ascend 符号链接）：flagtree 0.6.0 fork 与 torch_npu
# 2.8.0.post2 的 API 差异（triton_key 缺失 / launch hook 类属性 / constexpr
# launcher 参数 / extra 目录命名），缺失则任何 torch.compile 与 CUDA graph
# capture 都 NameError。本脚本以文件末尾 marker 注释为锚点幂等重打，供
# install_component.py（安装后自动 --apply）与 start_service.sh（启动前
# --verify，缺失则 --apply）两个防线调用。patch 5-7 与 symlink 仅对 flagtree
# 栈生效（triton 包内存在 FLAGTREE_BACKEND 标记文件）。
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

# 补丁定义（| 分隔）：
#   常规补丁：目标文件(相对 site-packages) | marker 锚点 | patch 文件名 | 适用条件(空=无条件；flagtree=仅 flagtree 栈)
#   符号链接：父目录(相对 site-packages) | 链接名 | 链接目标 | 适用条件 | symlink
PATCHES=(
    "triton/language/extra/cann/libdevice.py|flagos-patch-1-triton-libdevice-pow|patch_1_triton_libdevice_pow.patch"
    "flag_gems/runtime/backend/_ascend/ops/pow.py|flagos-patch-2-flag-gems-pow-dtype|patch_2_flag_gems_pow_dtype.patch"
    "flag_gems/runtime/backend/_ascend/__init__.py|flagos-patch-3-customized-unused-ops|patch_3_flag_gems_customized_unused_ops.patch"
    "flag_gems/__init__.py|flagos-patch-4-flag-gems-record-default|patch_4_flag_gems_record_default.patch"
    "triton/spec/ascend/compiler/compiler.py|flagos-patch-5-flagtree-triton-key|patch_5_flagtree_triton_key.patch|flagtree"
    "triton/spec/ascend/compiler/compiler.py|flagos-patch-6-flagtree-launch-hooks|patch_6_flagtree_launch_hooks.patch|flagtree"
    "torch_npu/_inductor/npu_triton_heuristics.py|flagos-patch-7-torch-npu-constexpr-launcher|patch_7_torch_npu_constexpr_launcher.patch|flagtree"
    "triton/language/extra|ascend|cann|flagtree|symlink"
    "torch_npu/_inductor/codegen/ir.py|flagos-patch-8-torch-npu-flattened-dims-keyerror|patch_8_torch_npu_flattened_dims_keyerror.patch"
    "torch_npu/_inductor/npu_triton_heuristics.py|flagos-patch-9-torch-npu-sync-during-capture|patch_9_torch_npu_sync_during_capture.patch"
    "torch/distributed/distributed_c10d.py|flagos-patch-10-dist-ephemeral-timeout-guard|patch_10_dist_ephemeral_timeout_guard.patch"
    "torch_npu/_inductor/npu_triton_heuristics.py|flagos-patch-11-torch-npu-autotune-capture-guard|patch_11_torch_npu_autotune_capture_guard.patch"
)

patch_file_of() { echo "$1" | cut -d'|' -f3; }
marker_of()     { echo "$1" | cut -d'|' -f2; }
target_of()     { echo "$SITE_PACKAGES/$(echo "$1" | cut -d'|' -f1)"; }
cond_of()       { echo "$1" | cut -d'|' -f4; }
action_of()     { echo "$1" | cut -d'|' -f5; }

# 条件判定：flagtree 栈 = triton 包内存在 FLAGTREE_BACKEND 标记文件（flagtree 0.6.x
# wheel 直接安装在 triton/ 内，无独立 flagtree 模块）
condition_met() {
    local cond
    cond="$(cond_of "$1")"
    case "$cond" in
        flagtree) [ -f "$SITE_PACKAGES/triton/FLAGTREE_BACKEND" ] ;;
        *) return 0 ;;
    esac
}

is_applied() {
    local target marker
    target="$(target_of "$1")"
    marker="$(marker_of "$1")"
    [ -f "$target" ] && grep -qF "$marker" "$target"
}

apply_one() {
    local spec="$1" target marker patchfile
    if [ "$(action_of "$spec")" = "symlink" ]; then
        apply_symlink "$spec"
        return $?
    fi
    target="$(target_of "$spec")"
    marker="$(marker_of "$spec")"
    patchfile="$PATCHES_DIR/$(patch_file_of "$spec")"

    if ! condition_met "$spec"; then
        echo "[apply_patches] SKIP  ${target}（条件不满足：非 flagtree 栈）"
        return 0
    fi
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
        if ! condition_met "$spec"; then
            echo "[apply_patches] SKIP  $(target_of "$spec")（条件不满足：非 flagtree 栈）"
            continue
        fi
        if [ "$(action_of "$spec")" = "symlink" ]; then
            link="$(symlink_parent_of "$spec")/$(symlink_name_of "$spec")"
            if [ -L "$link" ]; then
                echo "[apply_patches] OK    ${link}（符号链接）"
            else
                echo "[apply_patches] MISS  ${link}（符号链接缺失）"
                missing=1
            fi
            continue
        fi
        if is_applied "$spec"; then
            echo "[apply_patches] OK    $(target_of "$spec")"
        else
            echo "[apply_patches] MISS  $(target_of "$spec")"
            missing=1
        fi
    done
    return "$missing"
}

# 符号链接型补丁：flagtree fork 将 extra/ascend 命名为 extra/cann，
# torch_npu 期望 extra.ascend 命名 → 建立 ascend -> cann 别名链接
symlink_parent_of() { echo "$SITE_PACKAGES/$(echo "$1" | cut -d'|' -f1)"; }
symlink_name_of()   { echo "$1" | cut -d'|' -f2; }
symlink_target_of() { echo "$1" | cut -d'|' -f3; }

apply_symlink() {
    local spec="$1" parent name target link
    parent="$(symlink_parent_of "$spec")"
    name="$(symlink_name_of "$spec")"
    target="$(symlink_target_of "$spec")"
    link="$parent/$name"
    if ! condition_met "$spec"; then
        echo "[apply_patches] SKIP  ${link}（条件不满足：非 flagtree 栈）"
        return 0
    fi
    if [ -L "$link" ]; then
        echo "[apply_patches] SKIP  ${link}（符号链接已存在）"
        return 0
    fi
    if [ -e "$link" ]; then
        echo "[apply_patches] ERROR ${link}: 已存在同名实体（非符号链接），需人工处理" >&2
        return 1
    fi
    ln -sfn "$target" "$link"
    echo "[apply_patches] APPLY ${link} -> ${target} ✓"
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
            if [ "$(action_of "$spec")" = "symlink" ]; then
                rm -f "$(symlink_parent_of "$spec")/$(symlink_name_of "$spec")" 2>/dev/null || true
                continue
            fi
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
