"""FlagOS Plugin-only Workflow Engine

确定性工作流引擎，将 Claude Code 作为可替换的第一代 Analysis Agent Runtime。

模块结构：
- schemas: Context Schema v2 数据结构
- artifacts: Artifact 契约和 Registry
- gates: Gate Reducer（业务闸门归约）
- engine: 确定性工作流引擎、恢复、算子版本
- agent: Analysis Agent 协议、校验、Session
- domain: 领域执行器（准入、启动、精度、性能、发布）
"""

__version__ = "2.0.0"
