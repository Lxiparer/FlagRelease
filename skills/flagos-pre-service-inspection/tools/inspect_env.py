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
inspect_env.py — 合并环境检查脚本

一次运行完成全部环境检查，替代原来 10+ 次 docker exec 串行执行。
输出结构化 JSON，可直接写入 context.yaml。

Usage:
    python3 inspect_env.py --output-json    # 输出 JSON（供程序读取）
    python3 inspect_env.py --report         # 输出人类可读报告
    python3 inspect_env.py                  # 同时输出 JSON 和报告
"""

import argparse
import importlib
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def find_best_python():
    """探测最佳 Python 解释器（sglang 分支：非 conda，默认 /usr/local/python3.11.14/bin）"""
    candidates = [
        "/usr/local/python3.11.14/bin/python3",
        os.path.expanduser("~/miniconda3/bin/python3"),
        os.path.expanduser("~/anaconda3/bin/python3"),
    ]
    # 检查 PATH 中是否有更高优先级的 python3
    for c in candidates:
        if os.path.isfile(c):
            return c
    return sys.executable


# 如果当前解释器不是最佳的，用最佳解释器重新执行自身
if __name__ == '__main__' and not os.environ.get('_INSPECT_ENV_REEXEC'):
    best = find_best_python()
    if best != sys.executable and os.path.isfile(best):
        os.environ['_INSPECT_ENV_REEXEC'] = '1'
        try:
            os.execv(best, [best] + sys.argv)
        except OSError as e:
            print(f"[WARN] execv({best}) 失败: {e}，使用当前解释器继续", file=sys.stderr)


def run_cmd(cmd, timeout=30):
    """运行 shell 命令并返回 stdout"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except Exception:
        return ""


def check_execution_mode():
    """检测是否在容器内运行"""
    if os.path.exists("/.dockerenv"):
        return "container"
    try:
        with open("/proc/1/cgroup", "r") as f:
            if "docker" in f.read():
                return "container"
    except Exception:
        pass
    return "host"


def check_core_packages():
    """检查核心组件版本"""
    packages = {}
    for pkg_name, import_name in [("torch", "torch"), ("sglang", "sglang")]:
        try:
            mod = importlib.import_module(import_name)
            packages[pkg_name] = getattr(mod, "__version__", "installed")
        except ImportError:
            packages[pkg_name] = None
    # torch CUDA version
    try:
        import torch
        packages["torch_cuda"] = torch.version.cuda if hasattr(torch.version, "cuda") else None
    except Exception:
        packages["torch_cuda"] = None
    return packages


def check_flag_packages():
    """检查 flag 生态组件版本"""
    packages = {}
    for pkg_name, import_name in [
        ("flaggems", "flag_gems"),
        ("flagscale", "flag_scale"),
        ("flagcx", "flagcx"),
        ("sglang_plugin", "sglang_fl"),
    ]:
        try:
            mod = importlib.import_module(import_name)
            packages[pkg_name] = getattr(mod, "__version__", "installed")
        except ImportError:
            packages[pkg_name] = None
    return packages


def probe_flaggems_capabilities():
    """探测 FlagGems 运行时能力"""
    result = {
        "flaggems_installed": False,
        "capabilities": [],
        "enable_signature": "",
        "enable_params": [],
        "vendor_config_path": "",
        "sglang_plugin_installed": False,
        "plugin_has_dispatch": False,
        "probe_error": "",
        "gpu_compute_capability": "",
        "gpu_arch": "",
        "plugin_env_vars": {},
        "plugin_control": {},
        "oot_ops": [],
    }

    # GPU compute capability 探测
    try:
        import torch
        if torch.cuda.is_available():
            major, minor = torch.cuda.get_device_capability(0)
            result["gpu_compute_capability"] = f"{major}.{minor}"
            result["gpu_arch"] = f"sm_{major}{minor}"
    except Exception:
        pass

    # Plugin dispatch 环境变量探测（sglang 分支：SGLANG_FL_* 权威映射）
    for var in ["SGLANG_FL_FLAGOS_WHITELIST", "SGLANG_FL_FLAGOS_BLACKLIST",
                "SGLANG_FL_OOT_WHITELIST", "SGLANG_FL_OOT_BLACKLIST",
                "SGLANG_FL_PREFER", "SGLANG_FL_OOT_ENABLED",
                "SGLANG_FL_PER_OP", "SGLANG_FL_DISPATCH_DEBUG",
                "SGLANG_FL_STRICT"]:
        val = os.environ.get(var)
        if val is not None:
            result["plugin_env_vars"][var] = val

    # 探测 FlagGems
    try:
        import flag_gems

        result["flaggems_installed"] = True

        # enable() 签名
        if hasattr(flag_gems, "enable"):
            sig = inspect.signature(flag_gems.enable)
            result["enable_signature"] = str(sig)
            params = list(sig.parameters.keys())
            result["enable_params"] = params
            if "unused" in params:
                result["capabilities"].append("enable_unused")

        # only_enable()
        if hasattr(flag_gems, "only_enable"):
            result["capabilities"].append("only_enable")

        # use_gems 上下文管理器
        if hasattr(flag_gems, "use_gems"):
            result["capabilities"].append("use_gems")
            try:
                sig = inspect.signature(flag_gems.use_gems)
                params = list(sig.parameters.keys())
                if "include" in params or "exclude" in params:
                    result["capabilities"].append("use_gems_filter")
            except Exception:
                pass

        # YAML 配置支持
        if hasattr(flag_gems, "config"):
            cfg = flag_gems.config
            if hasattr(cfg, "resolve_user_setting"):
                result["capabilities"].append("yaml_config")
            if hasattr(cfg, "get_default_enable_config"):
                result["capabilities"].append("vendor_default")
                try:
                    path = cfg.get_default_enable_config()
                    result["vendor_config_path"] = str(path) if path else ""
                except Exception:
                    pass

        # 算子查询接口
        if hasattr(flag_gems, "all_registered_ops"):
            result["capabilities"].append("query_ops")
        elif hasattr(flag_gems, "all_ops"):
            result["capabilities"].append("query_ops_legacy")

    except ImportError:
        pass
    except Exception as e:
        result["probe_error"] = str(e)

    # 探测 sglang-plugin-FL（sglang 分支：Layer1=ATen 替换 / Layer2=fused kernels(bridge)）
    try:
        import sglang_fl

        result["sglang_plugin_installed"] = True
        try:
            from sglang_fl.dispatch.manager import OpManager
            result["plugin_has_dispatch"] = True
        except ImportError:
            pass

        # 探测 OOT 算子列表（sglang_fl.dispatch.bridge 下 *_bridge 后缀的 fused kernels）
        try:
            from sglang_fl.dispatch import bridge as oot_module
            oot_ops = [name for name in dir(oot_module)
                       if not name.startswith('_') and callable(getattr(oot_module, name, None))]
            result["oot_ops"] = oot_ops
        except (ImportError, AttributeError):
            # 兜底：使用已知的 OOT 算子列表
            result["oot_ops"] = [
                "rms_norm_bridge", "rotary_embedding_bridge", "silu_and_mul_bridge",
                "fused_moe_bridge", "topk_bridge", "gemma_rms_norm_bridge",
                "mrotary_embedding_bridge", "fla_fused_recurrent_bridge",
            ]

        # 构建 plugin_control 信息
        result["plugin_control"] = {
            "prefer": os.environ.get("SGLANG_FL_PREFER", "not_set"),
            "oot_enabled": os.environ.get("SGLANG_FL_OOT_ENABLED", "not_set"),
            "oot_ops": result["oot_ops"],
            "flagos_whitelist": os.environ.get("SGLANG_FL_FLAGOS_WHITELIST", ""),
            "flagos_blacklist": os.environ.get("SGLANG_FL_FLAGOS_BLACKLIST", ""),
            "oot_blacklist": os.environ.get("SGLANG_FL_OOT_BLACKLIST", ""),
            "per_op": os.environ.get("SGLANG_FL_PER_OP", ""),
        }
    except ImportError:
        pass

    return result


def scan_flaggems_integration():
    """多维度扫描 FlagGems 集成方式"""
    integration = {
        "env_vars": {},
        "code_locations": [],
        "entry_points": [],
        "startup_scripts": [],
        "integration_type": "unknown",
        "enable_method": "",
        "disable_method": "",
    }

    # 维度1：环境变量检查
    for var in ["USE_FLAGGEMS", "USE_FLAGOS", "FLAGGEMS_LOG_LEVEL", "ENABLE_FLAGGEMS"]:
        val = os.environ.get(var)
        if val is not None:
            integration["env_vars"][var] = val

    # 维度2：sglang_fl 插件（sglang 分支集成点；sglang 全包太大不扫）
    for framework in ["sglang_fl"]:
        try:
            mod = importlib.import_module(framework)
            fw_path = mod.__path__[0]
            output = run_cmd(
                f"grep -rn 'flag_gems\\|flaggems\\|use_gems\\|enable.*gems\\|import.*gems' {fw_path}/ 2>/dev/null"
            )
            if output:
                for line in output.strip().split("\n"):
                    if line:
                        integration["code_locations"].append(line)
        except (ImportError, Exception):
            pass

    # 维度3：入口点扫描（sglang 分支：sglang.srt.plugins 组）
    try:
        import pkg_resources
        for group in ["sglang.srt.plugins"]:
            for ep in pkg_resources.iter_entry_points(group):
                integration["entry_points"].append(f"{group}: {ep.name} = {ep}")
    except Exception:
        pass

    # 维度4：启动脚本扫描
    output = run_cmd(
        "find /usr/local/bin /opt /root -name '*.sh' -exec grep -l 'gems\\|flagos\\|flag_gems' {} \\; 2>/dev/null"
    )
    if output:
        integration["startup_scripts"] = [s for s in output.strip().split("\n") if s]

    # 推导集成方式
    _derive_integration_methods(integration)

    return integration


def _derive_integration_methods(integration):
    """根据扫描结果推导 FlagGems 启用/关闭方法"""
    code_locs = integration["code_locations"]
    env_vars = integration["env_vars"]
    entry_points = integration["entry_points"]

    # 优先级1：环境变量控制
    for var in ["USE_FLAGGEMS", "USE_FLAGOS"]:
        if var in env_vars:
            integration["integration_type"] = "env_var"
            integration["enable_method"] = f"env:{var}=1"
            integration["disable_method"] = f"env:{var}=0"
            return
    # 检查代码中是否引用了这些环境变量
    for loc in code_locs:
        for var in ["USE_FLAGGEMS", "USE_FLAGOS"]:
            if var in loc:
                integration["integration_type"] = "env_var"
                integration["enable_method"] = f"env:{var}=1"
                integration["disable_method"] = f"env:{var}=0"
                return

    # 优先级2：插件入口点
    if entry_points:
        integration["integration_type"] = "plugin"
        integration["enable_method"] = "auto"
        integration["disable_method"] = "env:USE_FLAGGEMS=0"
        return

    # 优先级3：代码中直接 import
    if code_locs:
        # 解析具体的代码位置
        import_locs = []
        for loc in code_locs:
            match = re.match(r"^(.+):(\d+):(.+)$", loc)
            if match:
                filepath, lineno, content = match.groups()
                if "import flag_gems" in content or "flag_gems.enable" in content:
                    import_locs.append({"file": filepath, "line": int(lineno), "content": content.strip()})

        if import_locs:
            integration["integration_type"] = "code_import"
            # 提供代码文件列表供 toggle_flaggems.py 使用
            files = list(set(loc["file"] for loc in import_locs))
            integration["enable_method"] = f"code:uncomment:{json.dumps(files)}"
            integration["disable_method"] = f"code:comment:{json.dumps(files)}"
            integration["code_import_details"] = import_locs
            return

    # 优先级4：启动脚本
    if integration["startup_scripts"]:
        integration["integration_type"] = "script"
        integration["enable_method"] = f"script:{integration['startup_scripts'][0]}"
        integration["disable_method"] = f"script:{integration['startup_scripts'][0]}"
        return

    # 无法确定
    integration["integration_type"] = "unknown"
    integration["enable_method"] = "unknown"
    integration["disable_method"] = "unknown"


def check_env_vars():
    """列出所有 flag 相关环境变量"""
    result = {}
    for key, val in os.environ.items():
        if re.search(r"flag|gems|flagos", key, re.IGNORECASE):
            result[key] = val
    return result


def classify_env_type(capabilities, integration):
    """根据 flaggems 和 plugin 安装情况分类环境场景（sglang 分支收敛为二态）

    sglang 无代码注入态：算子控制统一走 SGLANG_FL_* 环境变量，控制面由
    sglang_fl 插件提供。无 plugin 的 flaggems 无法经框架控制 → 归 native。

    Returns:
        str: native | sglang_plugin_flaggems
    """
    flaggems_installed = capabilities.get("flaggems_installed", False)
    plugin_installed = capabilities.get("sglang_plugin_installed", False)

    if not flaggems_installed:
        return "native"
    elif plugin_installed:
        return "sglang_plugin_flaggems"
    else:
        # sglang 分支：无代码注入态，flaggems 无 plugin 控制面 → 按 native 处理
        return "native"


def classify_entry_image_type(capabilities, flagtree):
    """准入镜像分类 — 决定走哪条 pipeline（sglang 分支收敛为二态）。

    sglang 无代码注入态，分支 A（gems_tree 代码注入）不适用：
      - gems_tree_plugin : flaggems + plugin                    → 分支 B（复杂路径）
      - native           : 无 flaggems（或无 plugin 的 flaggems）→ native 简化流程

    Returns:
        dict: {
            entry_image_type: str,
            has_flaggems: bool,
            has_flagtree: bool,
            has_plugin: bool,
            pipeline_branch: str,   # B | native | ""
            reason: str,
        }
    """
    has_flaggems = capabilities.get("flaggems_installed", False)
    has_plugin = capabilities.get("sglang_plugin_installed", False)
    has_flagtree = bool(flagtree.get("installed", False))

    if not has_flaggems:
        entry_type = "native"
        branch = "native"
        reason = "未检测到 flaggems，走 native 简化流程"
    elif has_plugin:
        entry_type = "gems_tree_plugin"
        branch = "B"
        reason = "flaggems + plugin（+tree）预装，走分支 B（复杂路径，V1 二选/V2 分支）"
    else:
        # sglang 分支：无代码注入态，无 plugin 的 flaggems 无法经框架控制 → 按 native 处理
        entry_type = "native"
        branch = "native"
        reason = "flaggems 存在但无 plugin 控制面（sglang 无代码注入），按 native 处理"

    # flagtree 缺失不改变分类，仅记录（flaggems 是核心判据，见 CLAUDE.md 场景定义）
    if has_flaggems and not has_flagtree:
        reason += "；⚠ 未检测到 flagtree"

    return {
        "entry_image_type": entry_type,
        "has_flaggems": has_flaggems,
        "has_flagtree": has_flagtree,
        "has_plugin": has_plugin,
        "pipeline_branch": branch,
        "reason": reason,
    }


# =========================================================================
# 环境变量驱动算子控制（sglang 分支：无代码注入，统一走 SGLANG_FL_* env）
# =========================================================================

# ascend 兼容补丁（triton libdevice pow / flag_gems pow dtype / CUSTOMIZED_UNUSED_OPS /
# enable() record 默认）由 apply_patches.sh 维护，此处仅探测状态。
APPLY_PATCHES_SCRIPT = "/flagos-workspace/scripts/apply_patches.sh"


def probe_patch_status():
    """sglang 分支：探测 ascend 兼容补丁应用状态（apply_patches.sh --status）"""
    status = {"script_found": False, "patches": {}, "all_ok": False, "probe_error": ""}
    if not os.path.isfile(APPLY_PATCHES_SCRIPT):
        return status
    status["script_found"] = True
    try:
        out = subprocess.run(["bash", APPLY_PATCHES_SCRIPT, "--status"],
                             capture_output=True, text=True, timeout=60)
        for line in out.stdout.splitlines():
            m = re.match(r"\[apply_patches\] (OK|MISS)\s+(.+)", line)
            if m:
                state, target = m.groups()
                # key 用 site-packages 相对路径，避免同名 basename 覆盖（两个 __init__.py）
                key = target.split("site-packages/")[-1] if "site-packages/" in target else target.split("/")[-1]
                status["patches"][key] = "ok" if state == "OK" else "missing"
        status["all_ok"] = bool(status["patches"]) and all(v == "ok" for v in status["patches"].values())
    except Exception as e:
        status["probe_error"] = str(e)
    return status


def _write_control_env_vars(env_type):
    """sglang 分支：仅持久化 USE_FLAGGEMS 开关（无 FLAGGEMS_CONTROL_MODE 注入机制）"""
    use_flaggems = "1" if env_type == "sglang_plugin_flaggems" else "0"
    os.environ["USE_FLAGGEMS"] = use_flaggems

    env_path = "/etc/environment"
    try:
        existing = ""
        if os.path.isfile(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                existing = f.read()
        lines = [l for l in existing.split('\n')
                 if not l.startswith("USE_FLAGGEMS=")]
        lines.append(f"USE_FLAGGEMS={use_flaggems}")
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(l for l in lines if l is not None) + '\n')
        print(f"  ✓ 环境变量已写入 {env_path}: USE_FLAGGEMS={use_flaggems}", file=sys.stderr)
    except Exception as e:
        print(f"  WARN: 写入 {env_path} 失败: {e}", file=sys.stderr)

    return {"USE_FLAGGEMS": use_flaggems}


def check_flagtree():
    """检测 FlagTree 安装状态"""
    result = {
        "installed": False,
        "version": "",
        "triton_version": "",
        "backend": "",
    }
    try:
        import triton
        result["triton_version"] = getattr(triton, "__version__", "unknown")
    except ImportError:
        return result

    try:
        import flagtree
        result["installed"] = True
        result["version"] = getattr(flagtree, "__version__", "unknown")
        result["backend"] = getattr(flagtree, "backend", "")
    except ImportError:
        # triton 存在但非 FlagTree
        pass

    return result


def collect_all():
    """收集全部检查结果"""
    exec_mode = check_execution_mode()
    core = check_core_packages()
    flag = check_flag_packages()
    capabilities = probe_flaggems_capabilities()
    integration = scan_flaggems_integration()
    flagtree = check_flagtree()
    env_vars = check_env_vars()

    env_type = classify_env_type(capabilities, integration)
    caps = capabilities["capabilities"]

    # sglang 分支：无代码注入。算子控制统一走 SGLANG_FL_* 环境变量，
    # 仅持久化 USE_FLAGGEMS 开关；补丁状态由 apply_patches.sh --status 探测。
    control_env = _write_control_env_vars(env_type)
    patch_status = probe_patch_status()

    return {
        "execution": {
            "mode": exec_mode,
        },
        "inspection": {
            "core_packages": core,
            "flag_packages": flag,
            "flaggems_capabilities": caps,
            "flaggems_enable_signature": capabilities["enable_signature"],
            "flaggems_enable_params": capabilities["enable_params"],
            "vendor_config_path": capabilities["vendor_config_path"],
            "sglang_plugin_installed": capabilities["sglang_plugin_installed"],
            "plugin_has_dispatch": capabilities["plugin_has_dispatch"],
            "probe_error": capabilities["probe_error"],
            "gpu_compute_capability": capabilities["gpu_compute_capability"],
            "gpu_arch": capabilities["gpu_arch"],
            "plugin_env_vars": capabilities["plugin_env_vars"],
            "plugin_control": capabilities.get("plugin_control", {}),
            "oot_ops": capabilities.get("oot_ops", []),
            "patch_status": patch_status,
            "env_vars": env_vars,
        },
        "flagtree": flagtree,
        "flaggems_control": {
            "integration_type": integration["integration_type"],
            "enable_method": integration["enable_method"],
            "disable_method": integration["disable_method"],
            "code_locations": integration["code_locations"],
            "entry_points": integration["entry_points"],
            "startup_scripts": integration["startup_scripts"],
        },
        "env_classification": {
            "env_type": env_type,
            "has_flagtree": flagtree["installed"],
            "control_mechanism": "env_var(SGLANG_FL_*)" if env_type == "sglang_plugin_flaggems" else "none",
        },
        "entry_classification": classify_entry_image_type(capabilities, flagtree),
        "control_env": control_env,
    }


def output_json(data):
    """输出 JSON 格式"""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def output_report(data):
    """输出人类可读报告"""
    insp = data["inspection"]
    ctrl = data["flaggems_control"]

    report = []
    report.append("=" * 60)
    report.append("环境检测报告")
    report.append("=" * 60)

    report.append(f"\n## 执行模式: {data['execution']['mode']}")

    # 环境场景分类
    env_cls = data.get("env_classification", {})
    env_type = env_cls.get("env_type", "unknown")
    env_type_labels = {
        "native": "纯 sglang 原生（无 FlagGems 或无可控 plugin）",
        "sglang_plugin_flaggems": "sglang + plugin + flaggems（SGLANG_FL_* 环境变量控制）",
    }
    report.append(f"\n## 环境场景: {env_type} — {env_type_labels.get(env_type, '未知')}")
    if env_cls.get("has_flagtree"):
        report.append(f"  FlagTree:     已安装")

    # 准入镜像分类（双 pipeline 分发开关）
    entry_cls = data.get("entry_classification", {})
    if entry_cls:
        branch_labels = {"A": "分支 A（简单路径）", "B": "分支 B（复杂路径）",
                         "native": "native 简化流程", "": "待定"}
        report.append(f"\n## 准入镜像类型: {entry_cls.get('entry_image_type', 'unknown')} "
                      f"→ {branch_labels.get(entry_cls.get('pipeline_branch', ''), '未知')}")
        report.append(f"  判定依据: {entry_cls.get('reason', '-')}")
    # sglang 分支：无代码注入报告。控制机制 = SGLANG_FL_* env；展示补丁状态
    ctrl_env = data.get("control_env", {})
    if ctrl_env:
        report.append(f"\n## FlagGems 控制环境变量")
        report.append(f"  USE_FLAGGEMS:          {ctrl_env.get('USE_FLAGGEMS', '-')}")
    report.append(f"  控制机制: {env_cls.get('control_mechanism', 'none')}")

    patch_status = insp.get("patch_status", {})
    if patch_status.get("script_found"):
        report.append(f"\n## ascend 兼容补丁")
        for target, st in patch_status.get("patches", {}).items():
            mark = "✓" if st == "ok" else "✗"
            report.append(f"  {mark} {target}")
        report.append(f"  状态: {'全部就位' if patch_status.get('all_ok') else '存在缺失（启动前会自动重打）'}")
    else:
        report.append(f"\n## ascend 兼容补丁: 未部署 apply_patches.sh（跳过）")

    report.append("\n## 核心组件")
    report.append(f"  {'组件':<15} {'版本':<20} {'状态'}")
    report.append(f"  {'-'*15} {'-'*20} {'-'*10}")
    for pkg, ver in insp["core_packages"].items():
        if pkg == "torch_cuda":
            continue
        status = "已安装" if ver else "未安装"
        report.append(f"  {pkg:<15} {str(ver or '-'):<20} {status}")
    cuda_ver = insp["core_packages"].get("torch_cuda")
    if cuda_ver:
        report.append(f"  {'CUDA':<15} {cuda_ver:<20} {'已安装'}")

    report.append("\n## Flag 生态组件")
    report.append(f"  {'组件':<15} {'版本':<20} {'状态'}")
    report.append(f"  {'-'*15} {'-'*20} {'-'*10}")
    for pkg, ver in insp["flag_packages"].items():
        status = "已安装" if ver else "未安装"
        report.append(f"  {pkg:<15} {str(ver or '-'):<20} {status}")

    report.append("\n## FlagGems 集成分析")
    report.append(f"  集成方式:    {ctrl['integration_type']}")
    report.append(f"  启用方法:    {ctrl['enable_method']}")
    report.append(f"  关闭方法:    {ctrl['disable_method']}")
    report.append(f"  运行时能力:  {', '.join(insp['flaggems_capabilities']) or '无'}")
    if insp["flaggems_enable_signature"]:
        report.append(f"  enable() 签名: {insp['flaggems_enable_signature']}")

    if insp.get("gpu_compute_capability"):
        report.append(f"  GPU Compute:    {insp['gpu_compute_capability']} ({insp.get('gpu_arch', '')})")

    if insp.get("plugin_env_vars"):
        report.append(f"  Plugin 环境变量:")
        for k, v in insp["plugin_env_vars"].items():
            report.append(f"    {k}={v}")

    if insp.get("plugin_control"):
        pc = insp["plugin_control"]
        report.append(f"\n  Plugin 控制信息:")
        report.append(f"    prefer_enabled: {pc.get('prefer_enabled', 'not_set')}")
        report.append(f"    oot_enabled:    {pc.get('oot_enabled', 'not_set')}")
        if pc.get("oot_ops"):
            report.append(f"    OOT 算子:       {', '.join(pc['oot_ops'])}")
        if pc.get("per_op"):
            report.append(f"    per_op:         {pc['per_op']}")

    if ctrl["code_locations"]:
        report.append("\n  代码级扫描结果:")
        for loc in ctrl["code_locations"][:10]:
            report.append(f"    {loc}")

    if insp["env_vars"]:
        report.append("\n## 环境变量")
        for k, v in insp["env_vars"].items():
            report.append(f"  {k}={v}")
    else:
        report.append("\n## 环境变量: 无 flag 相关环境变量")

    # FlagTree
    ft = data.get("flagtree", {})
    report.append("\n## FlagTree")
    if ft.get("installed"):
        report.append(f"  状态:        已安装")
        report.append(f"  版本:        {ft.get('version', 'unknown')}")
        report.append(f"  Triton 版本: {ft.get('triton_version', 'unknown')}")
        if ft.get("backend"):
            report.append(f"  Backend:     {ft['backend']}")
    else:
        triton_ver = ft.get("triton_version", "")
        if triton_ver:
            report.append(f"  状态:        未安装（triton {triton_ver} 为原版）")
        else:
            report.append(f"  状态:        未安装（triton 也未安装）")

    if insp["probe_error"]:
        report.append(f"\n## 探测错误: {insp['probe_error']}")

    report.append("\n" + "=" * 60)
    print("\n".join(report))


def detect_model_dtype(model_path: str) -> str:
    """从模型 config.json 读取权重数制（torch_dtype）。"""
    if not model_path:
        return ""
    config_json = os.path.join(model_path, "config.json")
    if not os.path.exists(config_json):
        # 尝试常见子目录
        for sub in ["", "config"]:
            p = os.path.join(model_path, sub, "config.json") if sub else config_json
            if os.path.exists(p):
                config_json = p
                break
        else:
            return ""
    try:
        with open(config_json, "r") as f:
            cfg = json.load(f)
        return cfg.get("torch_dtype", "")
    except Exception:
        return ""


def main():
    parser = argparse.ArgumentParser(description="FlagOS 环境检查合并脚本")
    parser.add_argument("--output-json", action="store_true", help="输出 JSON 格式")
    parser.add_argument("--report", action="store_true", help="输出人类可读报告")
    parser.add_argument("--model-path", default="", help="模型路径，用于检测权重数制 (torch_dtype)")
    args = parser.parse_args()

    data = collect_all()

    # 追加模型权重数制检测
    if args.model_path:
        dtype = detect_model_dtype(args.model_path)
        if dtype:
            data["model_dtype"] = dtype

    if args.output_json:
        output_json(data)
    elif args.report:
        output_report(data)
    else:
        # 默认都输出
        output_json(data)
        print("\n---\n")
        output_report(data)


if __name__ == "__main__":
    main()
