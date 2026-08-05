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

"""芯片厂商×型号统一规范模块。

唯一权威数据源：同目录下 chip_spec.yaml。
检测(detect_gpu.py)、命名(config.py/get_image_name.sh)、报告(generate_report.py)
均通过本模块取值，消灭历史上散落且互相矛盾的厂商写法。

对外函数：
  normalize_vendor(raw)          别名/大小写 → 规范 vendor key（未知返回原值小写）
  canonical_chip(vendor, model)  原始 smi 型号 → 规范显示名（未命中返回原值）
  naming_suffix(vendor)          命名后缀 xxx-{suffix}-FlagOS
  vendor_display(vendor)         报告展示 "中文名(英文名)"
  vendor_en(vendor) / vendor_cn(vendor)
  valid_vendor_keys()            全部规范 vendor key 列表
"""
import os
from functools import lru_cache
from typing import Dict, List, Optional

_SPEC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chip_spec.yaml")


@lru_cache(maxsize=1)
def _load_spec() -> Dict[str, dict]:
    """加载规范表。yaml 缺失或解析失败时返回空 dict（调用方自带兜底，不抛错中断流程）。"""
    try:
        import yaml
    except ImportError:
        return {}
    try:
        with open(_SPEC_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _alias_index() -> Dict[str, str]:
    """构建 alias（含 key 自身）→ 规范 vendor key 的反查表，全部小写。"""
    idx: Dict[str, str] = {}
    for key, spec in _load_spec().items():
        k = key.strip().lower()
        idx[k] = k
        for alias in (spec.get("aliases") or []):
            a = str(alias).strip().lower()
            if a:
                idx[a] = k
    return idx


def normalize_vendor(raw: Optional[str]) -> str:
    """别名/大小写归一到规范 vendor key。未知则返回原值小写去空格。"""
    if not raw:
        return ""
    v = str(raw).strip().lower()
    return _alias_index().get(v, v)


def valid_vendor_keys() -> List[str]:
    """全部规范 vendor key。"""
    return list(_load_spec().keys())


def naming_suffix(vendor: Optional[str]) -> str:
    """命名后缀：xxx-{suffix}-FlagOS。未知厂商回退归一化后的 key。"""
    key = normalize_vendor(vendor)
    spec = _load_spec().get(key, {})
    return spec.get("naming_suffix", key) or key


def vendor_en(vendor: Optional[str]) -> str:
    """报告用英文名。未知回退归一化 key。"""
    key = normalize_vendor(vendor)
    spec = _load_spec().get(key, {})
    return spec.get("vendor_en", key) or key


def vendor_cn(vendor: Optional[str]) -> str:
    """厂商中文名。未知回退空。"""
    key = normalize_vendor(vendor)
    return _load_spec().get(key, {}).get("vendor_cn", "") or ""


def vendor_display(vendor: Optional[str]) -> str:
    """报告展示：有中文名则 "中文名(英文名)"，否则仅英文名。"""
    key = normalize_vendor(vendor)
    if not key:
        return "-"
    en = vendor_en(key)
    cn = vendor_cn(key)
    return f"{cn}({en})" if cn else en


def canonical_chip(vendor: Optional[str], gpu_model: Optional[str]) -> str:
    """原始采集型号 → 规范显示名。

    按 match 关键字长度降序匹配（长的优先，H20-3e 先于 H20）。
    未命中返回原始 gpu_model（保持信息不丢）。
    """
    if not gpu_model:
        return gpu_model or ""
    key = normalize_vendor(vendor)
    spec = _load_spec().get(key, {})
    chips = spec.get("chips") or []
    norm = str(gpu_model).lower().replace(" ", "").replace("_", "")

    # 收集 (match关键字, display)，按关键字长度降序，保证最具体的型号先命中
    candidates = []
    for chip in chips:
        display = chip.get("display", "")
        for m in (chip.get("match") or []):
            candidates.append((str(m).lower().replace(" ", "").replace("_", ""), display))
    candidates.sort(key=lambda x: -len(x[0]))

    for m, display in candidates:
        if m and m in norm:
            return display
    return str(gpu_model)
