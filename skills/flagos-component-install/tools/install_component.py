#!/usr/bin/env python3

# Copyright 2026 FlagOS Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
install_component.py — FlagOS 生态组件统一安装/升级/卸载

支持组件：flaggems（sglang 分支 flagtree 硬闸门禁用——误装会卸载 triton-cx）
FlagGems 安装/升级成功后自动触发 apply_patches.sh --apply（三手工补丁保护第一防线）。

Usage:
    # FlagGems 安装（最新版）
    python install_component.py --component flaggems --action install --json

    # FlagGems 指定版本
    python install_component.py --component flaggems --action install --version 4.2.1rc0 --json

    # FlagTree 安装
    python install_component.py --component flagtree --action install --vendor nvidia --json

    # FlagTree 卸载
    python install_component.py --component flagtree --action uninstall --json

    # FlagTree 验证
    python install_component.py --component flagtree --action verify --json
"""

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

# error_writer 集成
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from error_writer import write_last_error, write_checkpoint
except ImportError:
    def write_last_error(*a, **kw): pass
    def write_checkpoint(*a, **kw): pass


# pip 包名映射
PACKAGE_NAMES = {
    "flaggems": "flag-gems",
}

# install_flagtree.sh 路径（容器内）
FLAGTREE_SCRIPT = str(Path(__file__).resolve().parent / "install_flagtree.sh")
if not os.path.isfile(FLAGTREE_SCRIPT):
    # 容器内扁平部署路径
    FLAGTREE_SCRIPT = "/flagos-workspace/scripts/install_flagtree.sh"


def run_cmd(cmd, timeout=300, env=None):
    """运行命令，返回 (returncode, stdout, stderr)"""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, env=merged_env
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "command timed out"
    except Exception as e:
        return -1, "", str(e)


def get_current_version(component):
    """获取当前安装版本"""
    pkg_name = PACKAGE_NAMES.get(component, component)
    code, out, err = run_cmd(f"pip show {pkg_name} 2>/dev/null")
    if code == 0:
        for line in out.split("\n"):
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    return None


def install_flaggems(version=None, proxy=None):
    """FlagGems 安装（pip install 最新版或指定版本）"""
    pkg_name = PACKAGE_NAMES["flaggems"]
    old_version = get_current_version("flaggems")

    env = {}
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy

    if version:
        cmd = f"pip install {pkg_name}=={version}"
    else:
        cmd = f"pip install {pkg_name}"

    code, out, err = run_cmd(cmd, timeout=300, env=env)

    result = {
        "component": "flaggems",
        "action": "install",
        "previous_version": old_version,
        "current_version": get_current_version("flaggems") if code == 0 else None,
        "install_method": "pip",
        "success": code == 0,
        "message": f"pip install 成功: {cmd}" if code == 0 else f"pip install 失败: {err}",
        "timestamp": datetime.now().isoformat(),
    }

    if result["success"]:
        result["api"] = check_flaggems_api()
        result["patches"] = _reapply_patches()   # 补丁保护第一防线

    return result


def upgrade_flaggems(proxy=None):
    """FlagGems 升级到最新版"""
    pkg_name = PACKAGE_NAMES["flaggems"]
    old_version = get_current_version("flaggems")

    env = {}
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy

    cmd = f"pip install --upgrade {pkg_name}"
    code, out, err = run_cmd(cmd, timeout=300, env=env)

    result = {
        "component": "flaggems",
        "action": "upgrade",
        "previous_version": old_version,
        "current_version": get_current_version("flaggems") if code == 0 else None,
        "install_method": "pip",
        "success": code == 0,
        "message": f"升级成功: {cmd}" if code == 0 else f"升级失败: {err}",
        "timestamp": datetime.now().isoformat(),
    }

    if result["success"]:
        result["api"] = check_flaggems_api()
        result["patches"] = _reapply_patches()   # 补丁保护第一防线

    return result


# =============================================================================
# FlagTree 操作（sglang 分支：硬闸门，不委托 install_flagtree.sh）
# =============================================================================

def handle_flagtree(action, vendor=None, version=None, source=False, branch=None, json_output=False):
    """FlagTree 操作（sglang 分支硬闸门：一律 skipped，不执行安装）。

    sglang 环境的 FlagCX 运行时是 triton-cx 3.5.1，安装 flagtree 会与其依赖冲突
    并卸载 triton-cx（灾难性）。任何 flagtree 操作直接返回 skipped，不调用
    install_flagtree.sh。本函数保留签名以兼容调用方，内部不再执行任何命令。
    """
    return {
        "component": "flagtree",
        "action": action,
        "success": True,
        "skipped": True,
        "message": (
            "sglang 分支禁用 flagtree：环境依赖 triton-cx（FlagCX 运行时），"
            "安装 flagtree 会卸载 triton-cx。如需底层编译器能力请用 triton-cx。"
        ),
        "timestamp": datetime.now().isoformat(),
    }


# =============================================================================
# 补丁恢复（FlagGems 安装/升级后自动调用）
# =============================================================================

APPLY_PATCHES_SCRIPT = "/flagos-workspace/scripts/apply_patches.sh"


def _reapply_patches():
    """FlagGems 安装/升级成功后重新应用手工补丁（sglang 分支第一防线）。

    pip install/upgrade 会覆盖 flag_gems/triton 文件，冲掉 4 个手工补丁
    （libdevice.py pow、flag_gems pow dtype、cat/exponential_、enable() record）。
    此处直接调用 apply_patches.sh --apply（幂等，marker 锚点检测）。
    """
    if not os.path.isfile(APPLY_PATCHES_SCRIPT):
        return {
            "applied": False,
            "message": f"apply_patches.sh 不存在: {APPLY_PATCHES_SCRIPT}（跳过，start_service 启动前会兜底校验）",
        }
    code, out, err = run_cmd(f"bash {APPLY_PATCHES_SCRIPT} --apply", timeout=120)
    return {
        "applied": code == 0,
        "message": out or err or ("补丁已恢复" if code == 0 else "补丁恢复失败"),
    }


# =============================================================================
# API 兼容性检查（FlagGems 安装/升级后）
# =============================================================================

def check_flaggems_api():
    """检查 FlagGems API 兼容性"""
    code, out, err = run_cmd("""python3 -c "
import json, flag_gems
result = {
    'version': getattr(flag_gems, '__version__', 'unknown'),
    'has_enable': hasattr(flag_gems, 'enable'),
    'has_only_enable': hasattr(flag_gems, 'only_enable'),
    'has_use_gems': hasattr(flag_gems, 'use_gems'),
}
if hasattr(flag_gems, 'enable'):
    import inspect
    sig = inspect.signature(flag_gems.enable)
    result['enable_params'] = list(sig.parameters.keys())
print(json.dumps(result, indent=2))
" """)
    if code == 0:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
    return {"error": err or "无法检查 API"}


# =============================================================================
# 主入口
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="FlagOS 组件统一安装工具")
    parser.add_argument("--component", required=True,
                        choices=["flaggems", "flagtree"],
                        help="要操作的组件")
    parser.add_argument("--action", required=True,
                        choices=["install", "upgrade", "uninstall", "verify"],
                        help="操作类型")
    parser.add_argument("--version", help="指定版本（如 4.2.1rc0）")
    parser.add_argument("--proxy", help="代理地址")
    parser.add_argument("--vendor", help="FlagTree 后端（如 nvidia, ascend）")
    parser.add_argument("--source", action="store_true", help="FlagTree 源码编译")
    parser.add_argument("--branch", help="FlagTree Git 分支")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    # FlagTree 委托给 install_flagtree.sh
    if args.component == "flagtree":
        result = handle_flagtree(
            action=args.action,
            vendor=args.vendor,
            version=args.version,
            source=args.source,
            branch=args.branch,
            json_output=args.json,
        )
    elif args.action == "verify":
        version = get_current_version(args.component)
        result = {
            "component": args.component,
            "action": "verify",
            "installed": version is not None,
            "version": version,
            "timestamp": datetime.now().isoformat(),
        }
        if args.component == "flaggems" and version:
            result["api"] = check_flaggems_api()
    elif args.action == "uninstall":
        pkg_name = PACKAGE_NAMES.get(args.component, args.component)
        code, out, err = run_cmd(f"pip uninstall -y {pkg_name}")
        result = {
            "component": args.component,
            "action": "uninstall",
            "success": code == 0,
            "message": out if code == 0 else err,
            "timestamp": datetime.now().isoformat(),
        }
    elif args.component == "flaggems":
        if args.action == "upgrade":
            result = upgrade_flaggems(proxy=args.proxy)
        else:
            result = install_flaggems(version=args.version, proxy=args.proxy)
    else:
        result = {
            "component": args.component,
            "action": args.action,
            "success": False,
            "message": f"不支持的组件: {args.component}",
            "timestamp": datetime.now().isoformat(),
        }

    # 输出
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        component = result.get("component", args.component)
        action = result.get("action", args.action)
        success = result.get("success", False)

        print(f"\nFlagOS 组件操作 — {component} {action}")
        print("=" * 50)

        if action == "verify":
            if args.component == "flagtree":
                print(f"  FlagTree: skipped（sglang 分支禁用，依赖 triton-cx 而非 flagtree）")
            else:
                print(f"  版本: {result.get('version', '未安装')}")
                api = result.get("api", {})
                if api and not api.get("error"):
                    print(f"  API: enable={api.get('has_enable')}, only_enable={api.get('has_only_enable')}")
        else:
            status = "成功" if success else "失败"
            print(f"  状态: {status}")
            print(f"  方式: {result.get('install_method', '-')}")
            if result.get("previous_version"):
                print(f"  版本: {result['previous_version']} → {result.get('current_version', '?')}")
            elif result.get("current_version"):
                print(f"  版本: {result['current_version']}")
            if result.get("message"):
                print(f"  信息: {result['message'][:200]}")

    sys.exit(0 if result.get("success", False) else 1)


if __name__ == "__main__":
    try:
        write_checkpoint("01_container_preparation", "组件安装", "running_install_component",
                         action_detail=" ".join(sys.argv))
        main()
    except Exception as e:
        write_last_error(
            tool="install_component.py",
            error_type=type(e).__name__,
            error_message=str(e),
            traceback_str=traceback.format_exc(),
        )
        print(f"[FATAL] install_component.py 异常退出: {e}")
        traceback.print_exc()
        sys.exit(1)
