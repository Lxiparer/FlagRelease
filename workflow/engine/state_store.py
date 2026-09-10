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

"""StateStore - 工作流状态持久化后端抽象

背景（见 memory state-storage-decision）：
本阶段暂守 plan 原案，状态存于 context.yaml（YAML）。但通过 StateStore 接口
把「怎么存」与「引擎逻辑」解耦——将来换 SQLite 事实库时接口不变、只换后端实现。

关键约束：
- 引擎是 context 的唯一写入者（save 只应由 WorkflowEngine._save_context 调用）。
- 每次 save 前经 validate_context_dict 校验（字段越权 + 篡改不可变数据）——
  这是 YAML 阶段的「字段权限 / append-only」雏形，替代存储层的强制约束。
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import yaml

from ..schemas.context_v2 import validate_context_dict


class StateStore(ABC):
    """状态存储后端抽象接口"""

    @abstractmethod
    def exists(self) -> bool:
        """状态是否已存在"""
        raise NotImplementedError

    @abstractmethod
    def load(self) -> Optional[dict]:
        """加载状态 dict，不存在返回 None"""
        raise NotImplementedError

    @abstractmethod
    def save(self, data: dict) -> None:
        """保存状态 dict（写入前必须校验）"""
        raise NotImplementedError


class YamlStateStore(StateStore):
    """YAML 文件后端（本阶段实现）"""

    def __init__(self, context_file: Path):
        self.context_file = Path(context_file)

    def exists(self) -> bool:
        return self.context_file.exists()

    def load(self) -> Optional[dict]:
        if not self.context_file.exists():
            return None
        with open(self.context_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def save(self, data: dict) -> None:
        # 读取磁盘上的旧状态用于篡改校验（唯一写入者模型下即上一次写入）
        old = self.load()
        validate_context_dict(data, old)

        self.context_file.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写临时文件再 rename，避免中断产生半截 YAML
        tmp = self.context_file.with_suffix(self.context_file.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        tmp.replace(self.context_file)


# --- 将来阶段占位（不在本轮实现）---------------------------------------
# class SqliteStateStore(StateStore):
#     """SQLite 事实库后端：单一可信事实源 + task 级字段写权限 + 历史 append-only。
#     引擎稳定后引入，接口与 YamlStateStore 一致，仅替换后端。
#     见 memory state-storage-decision。"""
#     ...
