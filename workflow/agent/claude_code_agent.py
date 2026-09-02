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

"""Claude Code Analysis Agent - 基于 Claude Code 的第一代 Analysis Agent

实现策略：
1. 将结构化 request 转换为 bounded prompt
2. 通过文件系统传递请求和证据（避免 API 直接调用的复杂性）
3. Claude Code 读取 Artifacts、生成结构化 JSON 输出
4. 解析 JSON 为 AnalysisResult
"""

import os
import json
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import logging

from .protocol import (
    AnalysisAgent,
    StartupFailureRequest,
    AccuracyRegressionRequest,
    UnknownFailureRequest,
    AnalysisResult,
    SuspectedOperator,
    RecommendedExperiment,
)
from ..artifacts.registry import ArtifactRegistry


class ClaudeCodeAnalysisAgent(AnalysisAgent):
    """Claude Code 实现的 Analysis Agent

    通过文件系统交互：
    - 写入 /flagos-workspace/agent_requests/<request_id>.json
    - Claude Code 读取 request + Artifacts
    - Claude Code 写入 /flagos-workspace/agent_results/<request_id>.json
    - 解析结果
    """

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        artifact_registry: Optional[ArtifactRegistry] = None,
    ):
        self.workspace_root = Path(workspace_root)
        self.requests_dir = self.workspace_root / "agent_requests"
        self.results_dir = self.workspace_root / "agent_results"

        # 确保目录存在
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

        self.artifact_registry = artifact_registry or ArtifactRegistry(str(workspace_root))
        self.logger = logging.getLogger("workflow.agent.claude_code")

    def analyze_startup_failure(self, request: StartupFailureRequest) -> AnalysisResult:
        """分析启动失败

        Args:
            request: 启动失败请求

        Returns:
            分析结果
        """
        self.logger.info(f"Analyzing startup failure for {request.operator_revision}")

        # 生成 request ID
        request_id = self._generate_request_id("startup", request.workflow_run_id)

        # 准备 prompt
        prompt = self._build_startup_failure_prompt(request)

        # 写入 request 文件
        self._write_request_file(request_id, request, prompt)

        # 调用 Claude Code（通过适配器或直接调用）
        result = self._invoke_claude_code(request_id, request, timeout=900)

        return result

    def analyze_accuracy_regression(self, request: AccuracyRegressionRequest) -> AnalysisResult:
        """分析精度退化

        Args:
            request: 精度退化请求

        Returns:
            分析结果
        """
        self.logger.info(
            f"Analyzing accuracy regression for {request.operator_revision}, "
            f"dataset={request.dataset}"
        )

        request_id = self._generate_request_id("accuracy", request.workflow_run_id)
        prompt = self._build_accuracy_regression_prompt(request)
        self._write_request_file(request_id, request, prompt)

        result = self._invoke_claude_code(request_id, request, timeout=900)

        return result

    def analyze_unknown_failure(self, request: UnknownFailureRequest) -> AnalysisResult:
        """分析未知故障

        Args:
            request: 未知故障请求

        Returns:
            分析结果
        """
        self.logger.info(f"Analyzing unknown failure at step {request.step_id}")

        request_id = self._generate_request_id("unknown", request.workflow_run_id)
        prompt = self._build_unknown_failure_prompt(request)
        self._write_request_file(request_id, request, prompt)

        result = self._invoke_claude_code(request_id, request, timeout=900)

        return result

    def get_capabilities(self) -> Dict[str, Any]:
        """获取 Claude Code Agent 能力

        Returns:
            能力描述
        """
        return {
            "agent_type": "claude_code",
            "version": "1.0",
            "structured_output": True,
            "tool_calling": True,
            "max_context_length": 200000,
            "max_tool_rounds": 50,
            "supported_analysis_types": [
                "startup_failure",
                "accuracy_regression",
                "unknown_failure",
            ],
        }

    def _generate_request_id(self, analysis_type: str, workflow_run_id: str) -> str:
        """生成 request ID

        格式: req-<type>-<timestamp>-<short_hash>
        """
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        import hashlib
        short_hash = hashlib.md5(f"{workflow_run_id}_{timestamp}".encode()).hexdigest()[:6]
        return f"req-{analysis_type}-{timestamp}-{short_hash}"

    def _build_startup_failure_prompt(self, request: StartupFailureRequest) -> str:
        """构造启动失败分析 prompt

        Args:
            request: 请求对象

        Returns:
            Bounded prompt
        """
        # 加载 Artifacts 摘要
        artifacts_summary = self._load_artifacts_summary(request.input_artifacts)

        # 算子约束
        discovered_set = request.operator_constraints.get('discovered_set', [])
        allow_fallback = request.operator_constraints.get('allow_fallback_to_installed_catalog', False)

        prompt = f"""你是 FlagOS 启动诊断专家。当前任务：分析 V3 启动崩溃，提出**可验证的算子禁用假设**。

## 上下文
- Workflow Run: {request.workflow_run_id}
- Operator Revision: {request.operator_revision}
- Candidate: {request.candidate}
- 当前已启用算子集: {len(discovered_set)} 个算子

## 输入证据（已登记的 Artifacts）
{artifacts_summary}

## 约束
- **候选算子必须属于已启用集**（{len(discovered_set)} 个算子）
"""

        if allow_fallback:
            prompt += """- **特殊情况**：若本轮新 oplist 尚未生成，候选可来自本轮直接证据（traceback/kernel/日志）并属于当前已安装 FlagGems 版本的已知算子目录
"""

        prompt += f"""- 最多提出 {request.limits.get('max_candidate_ops', 3)} 个候选
- 只能建议以下实验：{', '.join(request.allowed_experiments)}
- **必须引用 Artifact ID 作为证据**，不能凭空推断

## 输出格式（严格 JSON）
```json
{{
  "schema_version": "1.0",
  "agent_session_id": "as-<timestamp>-<hash>",
  "workflow_run_id": "{request.workflow_run_id}",
  "operator_revision": "{request.operator_revision}",
  "status": "hypothesis_available",
  "suspected_ops": [
    {{
      "name": "exact_operator_name",
      "confidence": 0.85,
      "evidence_artifacts": ["art-service-health-001"],
      "evidence_locations": ["service.log:1832-1874"],
      "reasoning": "Traceback shows crash in this operator"
    }}
  ],
  "recommended_experiment": {{
    "type": "disable_ops_and_restart",
    "ops": ["operator_name"],
    "parameters": {{}},
    "expected_outcome": "Service starts successfully without crash"
  }},
  "analysis_duration_seconds": 120.5,
  "tool_calls": 5
}}
```

**开始分析**：读取上述 Artifacts，定位问题算子，返回 JSON。
"""

        return prompt

    def _build_accuracy_regression_prompt(self, request: AccuracyRegressionRequest) -> str:
        """构造精度退化分析 prompt"""
        artifacts_summary = self._load_artifacts_summary(request.input_artifacts)
        discovered_set = request.operator_constraints.get('discovered_set', [])

        prompt = f"""你是 FlagOS 精度诊断专家。当前任务：分析精度退化，定位拖累精度的算子。

## 上下文
- Workflow Run: {request.workflow_run_id}
- Operator Revision: {request.operator_revision}
- Candidate: {request.candidate}
- Dataset: {request.dataset}
- 当前已启用算子集: {len(discovered_set)} 个算子

## 输入证据
{artifacts_summary}

## 约束
- 候选算子必须属于已启用集
- 最多提出 {request.limits.get('max_candidate_ops', 3)} 个候选
- 只能建议以下实验：{', '.join(request.allowed_experiments)}
- 必须引用 Artifact ID 作为证据

## 输出格式（严格 JSON）
```json
{{
  "schema_version": "1.0",
  "agent_session_id": "as-<timestamp>-<hash>",
  "workflow_run_id": "{request.workflow_run_id}",
  "operator_revision": "{request.operator_revision}",
  "status": "hypothesis_available",
  "suspected_ops": [
    {{
      "name": "exact_operator_name",
      "confidence": 0.75,
      "evidence_artifacts": ["art-accuracy-result-001"],
      "evidence_locations": [],
      "reasoning": "Known accuracy regression pattern"
    }}
  ],
  "recommended_experiment": {{
    "type": "disable_ops_and_restart",
    "ops": ["operator_name"],
    "parameters": {{}},
    "expected_outcome": "Accuracy improves to acceptable level"
  }},
  "analysis_duration_seconds": 90.0,
  "tool_calls": 3
}}
```

**开始分析**。
"""

        return prompt

    def _build_unknown_failure_prompt(self, request: UnknownFailureRequest) -> str:
        """构造未知故障分析 prompt"""
        artifacts_summary = self._load_artifacts_summary(request.input_artifacts)

        prompt = f"""你是 FlagOS 故障诊断专家。当前任务：分析未知故障，提供诊断建议。

## 上下文
- Workflow Run: {request.workflow_run_id}
- Step: {request.step_id}
- Failure Description: {request.failure_description}

## 输入证据
{artifacts_summary}

## 输出格式（严格 JSON）
```json
{{
  "schema_version": "1.0",
  "agent_session_id": "as-<timestamp>-<hash>",
  "workflow_run_id": "{request.workflow_run_id}",
  "operator_revision": "",
  "status": "hypothesis_available | no_hypothesis | unresolved",
  "suspected_ops": [],
  "recommended_experiment": null,
  "error_message": "",
  "analysis_duration_seconds": 60.0,
  "tool_calls": 2
}}
```

**开始分析**。
"""

        return prompt

    def _load_artifacts_summary(self, artifact_ids: List[str]) -> str:
        """加载 Artifacts 摘要（用于 prompt）

        Args:
            artifact_ids: Artifact IDs

        Returns:
            摘要文本
        """
        summaries = []

        for artifact_id in artifact_ids:
            artifact = self.artifact_registry.get_artifact(artifact_id)
            if not artifact:
                summaries.append(f"- {artifact_id}: [NOT FOUND]")
                continue

            metadata = artifact['metadata']
            content_summary = artifact.get('content_summary', {})

            summary_lines = [
                f"- **{artifact_id}** ({metadata['artifact_type']})",
                f"  - File: {metadata['file_path']}",
                f"  - Generated by: {metadata['generated_by']}",
            ]

            # 添加内容摘要
            if content_summary:
                for key, value in content_summary.items():
                    if isinstance(value, list):
                        summary_lines.append(f"  - {key}: {len(value)} items")
                    else:
                        summary_lines.append(f"  - {key}: {value}")

            summaries.append('\n'.join(summary_lines))

        return '\n\n'.join(summaries)

    def _write_request_file(
        self,
        request_id: str,
        request: Any,
        prompt: str,
    ):
        """写入 request 文件

        Args:
            request_id: Request ID
            request: 请求对象
            prompt: 生成的 prompt
        """
        request_file = self.requests_dir / f"{request_id}.json"

        request_data = {
            "request_id": request_id,
            "analysis_type": request.analysis_type,
            "workflow_run_id": request.workflow_run_id,
            "prompt": prompt,
            "request_details": request.__dict__,
            "created_at": datetime.now().isoformat(),
        }

        with open(request_file, 'w', encoding='utf-8') as f:
            json.dump(request_data, f, indent=2, ensure_ascii=False)

        self.logger.info(f"Written request file: {request_file}")

    def _invoke_claude_code(
        self,
        request_id: str,
        request: Any,
        timeout: int = 900,
    ) -> AnalysisResult:
        """调用 Claude Code 执行分析

        Args:
            request_id: Request ID
            request: 请求对象
            timeout: 超时时间（秒）

        Returns:
            AnalysisResult
        """
        # 方案：通过适配器脚本调用 Claude Code
        # 适配器会读取 request 文件，调用 Claude Code，写入 result 文件

        adapter_script = self.workspace_root.parent / "workflow" / "agent" / "claude_code_adapter.py"
        result_file = self.results_dir / f"{request_id}.json"

        # 暂时返回模拟结果（实际实现需要真正调用 Claude Code）
        self.logger.warning("Claude Code adapter not yet implemented, returning mock result")

        # 生成 mock result
        mock_result = AnalysisResult(
            schema_version="1.0",
            agent_session_id=f"as-{datetime.now().strftime('%Y%m%d%H%M%S')}-mock",
            workflow_run_id=request.workflow_run_id,
            operator_revision=getattr(request, 'operator_revision', ''),
            status="unresolved",
            error_message="Claude Code adapter not implemented",
            error_type="not_implemented",
        )

        return mock_result

    def _parse_result_file(self, result_file: Path) -> AnalysisResult:
        """解析 result 文件

        Args:
            result_file: 结果文件路径

        Returns:
            AnalysisResult
        """
        if not result_file.exists():
            return AnalysisResult(
                status="error",
                error_message="Result file not found",
                error_type="file_not_found",
            )

        with open(result_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # 解析 suspected_ops
        suspected_ops = []
        for op_data in data.get('suspected_ops', []):
            suspected_ops.append(SuspectedOperator(**op_data))

        # 解析 recommended_experiment
        recommended_experiment = None
        if data.get('recommended_experiment'):
            recommended_experiment = RecommendedExperiment(**data['recommended_experiment'])

        # 构造 AnalysisResult
        result = AnalysisResult(
            schema_version=data.get('schema_version', '1.0'),
            agent_session_id=data.get('agent_session_id', ''),
            workflow_run_id=data.get('workflow_run_id', ''),
            operator_revision=data.get('operator_revision', ''),
            status=data.get('status', 'unresolved'),
            suspected_ops=suspected_ops,
            recommended_experiment=recommended_experiment,
            error_message=data.get('error_message', ''),
            error_type=data.get('error_type', ''),
            analysis_duration_seconds=data.get('analysis_duration_seconds', 0.0),
            tool_calls=data.get('tool_calls', 0),
            _meta=data.get('_meta', {}),
        )

        return result
