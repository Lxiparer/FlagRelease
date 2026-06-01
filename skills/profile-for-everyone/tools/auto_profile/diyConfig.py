#!/usr/bin/env python3
"""Interactive config helper for auto_profile_script.

This script edits two files:

  local_required.json  - the tiny mandatory config, usually target + model_path
  override.json        - optional overrides; blank/null fields keep generated defaults

It intentionally avoids comments in JSON.  To understand the final effective
configuration, choose the print-config menu item; it calls profile_runner.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


MISSING = object()
LANG = "en"


def L(en: str, zh: str) -> str:
    return zh if LANG == "zh" else en


def choose_language() -> None:
    global LANG
    print("\nLanguage / 语言")
    print("  1. 中文")
    print("  2. English")
    ans = input("choose / 选择 [1]: ").strip()
    if ans in {"", "1", "zh", "cn", "中文"}:
        LANG = "zh"
    else:
        LANG = "en"


def load_json(path: Path, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def get_path(obj: Dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def ensure_path(obj: Dict[str, Any], dotted_parent: str) -> Dict[str, Any]:
    cur: Dict[str, Any] = obj
    if dotted_parent == "":
        return cur
    for part in dotted_parent.split("."):
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    return cur


def set_path(obj: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    parent = ensure_path(obj, ".".join(parts[:-1]))
    parent[parts[-1]] = value


def delete_path(obj: Dict[str, Any], dotted: str) -> None:
    parts = dotted.split(".")
    cur: Any = obj
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def parse_value(raw: str, kind: str = "auto") -> Any:
    raw = raw.strip()
    if raw == "":
        return None
    if kind == "str":
        return raw
    if kind == "int":
        return int(raw)
    if kind == "bool":
        low = raw.lower()
        if low in {"1", "true", "yes", "y", "on"}:
            return True
        if low in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError("Please input true/false")
    if kind == "json":
        return json.loads(raw)
    # auto
    low = raw.lower()
    if low in {"null", "none"}:
        return None
    if low in {"true", "false"}:
        return low == "true"
    try:
        if raw.startswith("[") or raw.startswith("{") or raw.startswith('"'):
            return json.loads(raw)
    except Exception:
        pass
    try:
        return int(raw)
    except ValueError:
        return raw


def input_line(prompt: str, default: Any = MISSING) -> str:
    suffix = ""
    if default is not MISSING and default is not None:
        suffix = f" [{default}]"
    return input(f"{prompt}{suffix}: ").strip()


def edit_field(data: Dict[str, Any], dotted: str, *, kind: str = "auto", label: Optional[str] = None, allow_delete: bool = True) -> None:
    label = label or dotted
    old = get_path(data, dotted, None)
    print(f"\n{label}")
    print(f"path: {dotted}")
    print(f"current: {json.dumps(old, ensure_ascii=False)}")
    raw = input_line(L("new value; empty = keep unchanged; '-' = clear/null", "输入新值；直接回车=不变；输入 '-' = 清空为 null/继续用默认"))
    if raw == "":
        print(L("unchanged", "未修改"))
        return
    if raw == "-" and allow_delete:
        set_path(data, dotted, None)
        print(L("set to null; defaults will be used", "已设为 null；运行时会继续使用默认值"))
        return
    try:
        value = parse_value(raw, kind)
    except Exception as e:
        print(f"[error] {e}")
        return
    set_path(data, dotted, value)
    print(L("updated", "已更新") + f": {dotted} = {json.dumps(value, ensure_ascii=False)}")


def choose(prompt: str, options: List[Tuple[str, str]]) -> Optional[str]:
    print("\n" + prompt)
    for key, text in options:
        print(f"  {key}. {text}")
    ans = input(L("choose", "选择") + ": ").strip()
    keys = {k for k, _ in options}
    if ans not in keys:
        print(L("invalid choice", "无效选项"))
        return None
    return ans


def edit_required(local: Dict[str, Any]) -> None:
    while True:
        ans = choose(L("Edit local_required.json", "编辑 local_required.json（必填项）"), [
            ("1", L("target: vendor / gems / both", "target：vendor / gems / both")),
            ("2", "release_name"),
            ("3", L("vendor model_path", "vendor 模型路径 model_path")),
            ("4", L("gems model_path", "gems 模型路径 model_path")),
            ("5", "served_model_name"),
            ("0", L("back", "返回")),
        ])
        if ans == "0" or ans is None:
            return
        if ans == "1":
            edit_field(local, "target", kind="str")
        elif ans == "2":
            edit_field(local, "release_name", kind="str")
        elif ans == "3":
            edit_field(local, "targets.vendor.model_path", kind="str")
        elif ans == "4":
            edit_field(local, "targets.gems.model_path", kind="str")
        elif ans == "5":
            edit_field(local, "served_model_name", kind="str")


def edit_side(override: Dict[str, Any], side: str) -> None:
    prefix = f"targets.{side}"
    while True:
        ans = choose(L(f"Edit override.json for {side}", f"编辑 {side} 的 override.json 可选覆盖项"), [
            ("1", "container.name"),
            ("2", "container.create_if_missing"),
            ("3", "container.start_if_stopped"),
            ("4", "container.stop_after_run"),
            ("5", "container.stop_timeout_s"),
            ("6", "runtime.workdir"),
            ("7", "runtime.python_bin_in_container"),
            ("8", "profile.output_db_name"),
            ("9", "profile.host_output_dir"),
            ("10", "override.port"),
            ("11", "override.served_model_name"),
            ("12", "override.model_path"),
            ("13", "requests.profile.payload.prompt"),
            ("14", "requests.profile.payload.max_tokens"),
            ("15", "timeouts.ready_s"),
            ("16", "logging.stream_vllm_log_to_console"),
            ("0", L("back", "返回")),
        ])
        if ans == "0" or ans is None:
            return
        mapping = {
            "1": (f"{prefix}.container.name", "str"),
            "2": (f"{prefix}.container.create_if_missing", "bool"),
            "3": (f"{prefix}.container.start_if_stopped", "bool"),
            "4": (f"{prefix}.container.stop_after_run", "bool"),
            "5": (f"{prefix}.container.stop_timeout_s", "int"),
            "6": (f"{prefix}.runtime.workdir", "str"),
            "7": (f"{prefix}.runtime.python_bin_in_container", "str"),
            "8": (f"{prefix}.profile.output_db_name", "str"),
            "9": (f"{prefix}.profile.host_output_dir", "str"),
            "10": (f"{prefix}.override.port", "int"),
            "11": (f"{prefix}.override.served_model_name", "str"),
            "12": (f"{prefix}.override.model_path", "str"),
            "13": (f"{prefix}.requests.profile.payload.prompt", "str"),
            "14": (f"{prefix}.requests.profile.payload.max_tokens", "int"),
            "15": (f"{prefix}.timeouts.ready_s", "int"),
            "16": (f"{prefix}.logging.stream_vllm_log_to_console", "bool"),
        }
        path, kind = mapping[ans]
        edit_field(override, path, kind=kind)


def edit_global_override(override: Dict[str, Any]) -> None:
    while True:
        ans = choose(L("Edit global override.json", "编辑全局 override.json 可选覆盖项"), [
            ("1", "release_name"),
            ("2", "served_model_name"),
            ("3", "port"),
            ("4", "host_output_dir"),
            ("5", "profile_request_count"),
            ("6", "max_tokens"),
            ("7", "profile_prompt"),
            ("8", "stop_after_run"),
            ("9", "create_if_missing"),
            ("10", "start_if_stopped"),
            ("0", L("back", "返回")),
        ])
        if ans == "0" or ans is None:
            return
        mapping = {
            "1": ("release_name", "str"),
            "2": ("served_model_name", "str"),
            "3": ("port", "int"),
            "4": ("host_output_dir", "str"),
            "5": ("profile_request_count", "int"),
            "6": ("max_tokens", "int"),
            "7": ("profile_prompt", "str"),
            "8": ("stop_after_run", "bool"),
            "9": ("create_if_missing", "bool"),
            "10": ("start_if_stopped", "bool"),
        }
        path, kind = mapping[ans]
        edit_field(override, path, kind=kind)


def print_effective_config(script_dir: Path, upstream: Path, local_required: Path, override: Path, target: str) -> None:
    cmd = [
        sys.executable,
        str(script_dir / "profile_runner.py"),
        "print-config",
        "--upstream", str(upstream),
        "--config", str(local_required),
        "--override", str(override),
        "--target", target,
    ]
    print("\n+ " + " ".join(cmd))
    subprocess.run(cmd, check=False)


def write_effective_config(script_dir: Path, upstream: Path, local_required: Path, override: Path, target: str, out: Path) -> None:
    cmd = [
        sys.executable,
        str(script_dir / "profile_runner.py"),
        "print-config",
        "--upstream", str(upstream),
        "--config", str(local_required),
        "--override", str(override),
        "--target", target,
    ]
    with out.open("w", encoding="utf-8") as f:
        subprocess.run(cmd, stdout=f, check=False)
    print(f"wrote {out}")


def show_release_name_note() -> None:
    if LANG == "zh":
        print("""
release_name 规则：
  - release_name 用来自动生成默认值，比如 container.name、runtime.workdir、profile.output_db_name。
  - 如果你又在 override.json 里显式设置了其中某个字段，显式值优先。
  - 例如：
      release_name = Qwen3-30B-A3B
      targets.vendor.container.name = minimax_vendor
    最终配置会使用 container.name=minimax_vendor，其它没覆盖的字段继续用 Qwen3-30B-A3B 推导出的默认值。
  - 不确定时，使用菜单里的“打印最终生效配置”检查。
""")
    else:
        print("""
release_name rule:
  - release_name is used to generate defaults such as container.name,
    runtime.workdir and profile.output_db_name.
  - If you also explicitly set one of those fields in override.json, your
    explicit value wins.
  - Therefore this is allowed:
      release_name = Qwen3-30B-A3B
      targets.vendor.container.name = minimax_vendor
    The final config will use container.name=minimax_vendor while other
    non-overridden defaults still use Qwen3-30B-A3B.
  - Always use print-config to inspect the final effective config.
""")


def main() -> None:
    ap = argparse.ArgumentParser(description="Interactive editor for local_required.json and override.json")
    ap.add_argument("--script-dir", default=None, help="auto_profile_script directory; default: this file's directory")
    ap.add_argument("--upstream", default="upstream.json")
    ap.add_argument("--local", default="local_required.json")
    ap.add_argument("--override", default="override.json")
    ap.add_argument("--target", choices=["both", "vendor", "gems"], default="both")
    args = ap.parse_args()

    script_dir = Path(args.script_dir).resolve() if args.script_dir else Path(__file__).resolve().parent
    upstream = Path(args.upstream)
    local_required = Path(args.local)
    override = Path(args.override)
    if not upstream.is_absolute():
        upstream = script_dir / upstream
    if not local_required.is_absolute():
        local_required = script_dir / local_required
    if not override.is_absolute():
        override = script_dir / override

    local = load_json(local_required, {"target": "both", "targets": {"vendor": {}, "gems": {}}})
    ov = load_json(override, {})

    choose_language()
    print(L("Auto Profile DIY Config", "自动 Profile 交互配置工具"))
    print(f"local_required: {local_required}")
    print(f"override:       {override}")
    show_release_name_note()

    dirty = False
    while True:
        ans = choose(L("Main menu", "主菜单"), [
            ("1", L("edit required fields: target / release_name / model_path", "编辑必填项：target / release_name / model_path")),
            ("2", L("edit global optional overrides", "编辑全局可选覆盖项")),
            ("3", L("edit vendor optional overrides", "编辑 vendor 可选覆盖项")),
            ("4", L("edit gems optional overrides", "编辑 gems 可选覆盖项")),
            ("5", L("set arbitrary override path, e.g. targets.vendor.container.name", "设置任意 override 路径，例如 targets.vendor.container.name")),
            ("6", L("clear arbitrary override path to null", "清空任意 override 路径为 null")),
            ("7", L("print effective config", "打印最终生效配置")),
            ("8", L("write effective config preview to final_config.preview.json", "把最终生效配置写入 final_config.preview.json")),
            ("9", L("save", "保存")),
            ("0", L("save and exit", "保存并退出")),
            ("q", L("quit without saving", "不保存退出")),
        ])
        if ans is None:
            continue
        if ans == "1":
            edit_required(local); dirty = True
        elif ans == "2":
            edit_global_override(ov); dirty = True
        elif ans == "3":
            edit_side(ov, "vendor"); dirty = True
        elif ans == "4":
            edit_side(ov, "gems"); dirty = True
        elif ans == "5":
            path = input_line(L("override dotted path, e.g. targets.vendor.container.name", "override 点分路径，例如 targets.vendor.container.name"))
            if path:
                edit_field(ov, path)
                dirty = True
        elif ans == "6":
            path = input_line(L("override dotted path to clear", "要清空的 override 点分路径"))
            if path:
                set_path(ov, path, None)
                print(L("set", "已设置") + f" {path} " + L("to null", "为 null"))
                dirty = True
        elif ans == "7":
            save_json(local_required, local)
            save_json(override, ov)
            dirty = False
            print_effective_config(script_dir, upstream, local_required, override, args.target)
        elif ans == "8":
            save_json(local_required, local)
            save_json(override, ov)
            dirty = False
            write_effective_config(script_dir, upstream, local_required, override, args.target, script_dir / "final_config.preview.json")
        elif ans == "9":
            save_json(local_required, local)
            save_json(override, ov)
            dirty = False
            print(L("saved", "已保存"))
        elif ans == "0":
            save_json(local_required, local)
            save_json(override, ov)
            print(L("saved", "已保存"))
            return
        elif ans == "q":
            if dirty:
                confirm = input_line(L("discard unsaved changes? type yes", "放弃未保存的修改？请输入 yes 确认"))
                if confirm != "yes":
                    continue
            print(L("quit without saving", "未保存并退出"))
            return


if __name__ == "__main__":
    main()
