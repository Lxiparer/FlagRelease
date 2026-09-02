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

"""Agent Session Manager - Agent 分析会话管理

职责：
1. 创建和跟踪 Agent 分析会话
2. 记录 Agent 输出和验证结果
3. 生成 session ID
4. 持久化 session 历史
"""

import json
import hashlib
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
import logging

from .protocol import (
    AgentSession,
    AnalysisResult,
    StartupFailureRequest,
    AccuracyRegressionRequest,
    UnknownFailureRequest,
)


class AgentSessionManager:
    """Agent 会话管理器"""

    def __init__(self, workspace_root: str = "/flagos-workspace"):
        self.workspace_root = Path(workspace_root)
        self.sessions_dir = self.workspace_root / "agent_sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        # 内存索引
        self.sessions: Dict[str, AgentSession] = {}

        self.logger = logging.getLogger("workflow.agent.session")

    def create_session(
        self,
        request: any,  # StartupFailureRequest | AccuracyRegressionRequest | UnknownFailureRequest
    ) -> AgentSession:
        """创建新的 Agent 分析会话

        Args:
            request: 分析请求

        Returns:
            AgentSession 对象
        """
        # 生成 session ID
        session_id = self._generate_session_id(request)

        # 创建 session
        session = AgentSession(
            session_id=session_id,
            request_type=request.analysis_type,
            workflow_run_id=request.workflow_run_id,
            operator_revision=getattr(request, 'operator_revision', ''),
            status="pending",
            started_at=datetime.now().isoformat(),
        )

        # 保存
        self.sessions[session_id] = session
        self._save_session(session)

        self.logger.info(f"Created agent session: {session_id}")

        return session

    def _generate_session_id(self, request: any) -> str:
        """生成 session ID

        格式: as-<YYYYMMDD>-<HHMMSS>-<type>-<short_hash>
        """
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        analysis_type_short = request.analysis_type.replace("_", "-")[:10]

        # 生成短哈希（基于 workflow_run_id + operator_revision）
        hash_input = f"{request.workflow_run_id}_{getattr(request, 'operator_revision', '')}_{timestamp}"
        short_hash = hashlib.md5(hash_input.encode()).hexdigest()[:6]

        return f"as-{timestamp}-{analysis_type_short}-{short_hash}"

    def update_session_result(
        self,
        session_id: str,
        result: AnalysisResult,
    ):
        """更新 session 的分析结果

        Args:
            session_id: Session ID
            result: 分析结果
        """
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        session.result = result
        session.status = result.status
        session.finished_at = datetime.now().isoformat()

        self._save_session(session)

        self.logger.info(f"Updated session {session_id} with result status: {result.status}")

    def update_verification_result(
        self,
        session_id: str,
        verification_status: str,
        verification_artifact: Optional[str] = None,
    ):
        """更新验证实验结果

        Args:
            session_id: Session ID
            verification_status: success / failed / not_executed
            verification_artifact: 验证结果 Artifact ID
        """
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        session.verification_status = verification_status
        session.verification_artifact = verification_artifact

        self._save_session(session)

        self.logger.info(
            f"Session {session_id} verification: {verification_status}, "
            f"artifact: {verification_artifact}"
        )

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        """获取 session

        Args:
            session_id: Session ID

        Returns:
            AgentSession 或 None
        """
        return self.sessions.get(session_id)

    def list_sessions(
        self,
        workflow_run_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[AgentSession]:
        """查询 sessions

        Args:
            workflow_run_id: 过滤指定 workflow run
            status: 过滤指定状态

        Returns:
            匹配的 sessions
        """
        results = []

        for session in self.sessions.values():
            if workflow_run_id and session.workflow_run_id != workflow_run_id:
                continue

            if status and session.status != status:
                continue

            results.append(session)

        return results

    def _save_session(self, session: AgentSession):
        """保存 session 到磁盘

        Args:
            session: AgentSession 对象
        """
        session_file = self.sessions_dir / f"{session.session_id}.json"

        # 转换为字典（简化，实际需要递归处理）
        session_dict = {
            "session_id": session.session_id,
            "request_type": session.request_type,
            "workflow_run_id": session.workflow_run_id,
            "operator_revision": session.operator_revision,
            "status": session.status,
            "started_at": session.started_at,
            "finished_at": session.finished_at,
            "result": session.result.__dict__ if session.result else None,
            "verification_status": session.verification_status,
            "verification_artifact": session.verification_artifact,
            "_meta": session._meta,
        }

        with open(session_file, 'w', encoding='utf-8') as f:
            json.dump(session_dict, f, indent=2, ensure_ascii=False)

    def load_sessions_from_disk(self):
        """从磁盘加载所有 sessions"""
        if not self.sessions_dir.exists():
            return

        for session_file in self.sessions_dir.glob("as-*.json"):
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    session_dict = json.load(f)

                # 简化反序列化（实际需要完整实现）
                session = AgentSession(
                    session_id=session_dict['session_id'],
                    request_type=session_dict['request_type'],
                    workflow_run_id=session_dict['workflow_run_id'],
                    operator_revision=session_dict['operator_revision'],
                    status=session_dict['status'],
                    started_at=session_dict['started_at'],
                    finished_at=session_dict.get('finished_at'),
                    verification_status=session_dict.get('verification_status'),
                    verification_artifact=session_dict.get('verification_artifact'),
                    _meta=session_dict.get('_meta', {}),
                )

                # 加载 result（如果有）
                # ... 需要完整实现

                self.sessions[session.session_id] = session

            except Exception as e:
                self.logger.warning(f"Failed to load session from {session_file}: {e}")

        self.logger.info(f"Loaded {len(self.sessions)} sessions from disk")

    def export_session_summary(self, session_id: str) -> Dict:
        """导出 session 摘要（用于报告）

        Args:
            session_id: Session ID

        Returns:
            摘要字典
        """
        session = self.get_session(session_id)
        if not session:
            return {}

        summary = {
            "session_id": session.session_id,
            "request_type": session.request_type,
            "operator_revision": session.operator_revision,
            "status": session.status,
            "started_at": session.started_at,
            "finished_at": session.finished_at,
        }

        if session.result:
            summary["suspected_ops"] = [
                {
                    "name": op.name,
                    "confidence": op.confidence,
                }
                for op in session.result.suspected_ops
            ]
            summary["recommended_experiment"] = (
                session.result.recommended_experiment.type
                if session.result.recommended_experiment else None
            )

        summary["verification_status"] = session.verification_status

        return summary
