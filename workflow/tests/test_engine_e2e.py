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

"""WorkflowEngine 端到端骨架测试（M0）

验证引擎能确定性驱动 15 步空跑、状态经 YAML 往返、非法/篡改写入被拦、
中断可从断点恢复。domain 执行器为 stub，不接容器 / 不接 Claude。
"""

import unittest
import sys
import tempfile
import shutil
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.engine.workflow_engine import WorkflowEngine, WORKFLOW_STEPS
from workflow.engine.state_store import YamlStateStore
from workflow.schemas.context_v2 import (
    ContextSchemaV2,
    ContextValidationError,
)


class TestEngineEndToEnd(unittest.TestCase):
    """引擎端到端空跑"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_run_all_15_steps(self):
        """run() 应驱动全部 15 步成功、终点停在 15_finalize"""
        engine = WorkflowEngine(self.tmpdir)
        ctx = engine.run()

        self.assertEqual(len(WORKFLOW_STEPS), 15)
        for step_id, _ in WORKFLOW_STEPS:
            self.assertEqual(
                ctx.steps[step_id].status, "success",
                f"step {step_id} 未成功: {ctx.steps[step_id].status}",
            )
        self.assertEqual(ctx.current_step_id, "15_finalize")
        # 冻结类步骤应产生冻结的 revision
        self.assertTrue(ctx.operator_revisions["v3-discovered"].frozen)
        self.assertTrue(ctx.operator_revisions["v3-final"].frozen)
        # finalize 应记录结束时间
        self.assertTrue(ctx.runtime.finished_at)

    def test_context_yaml_roundtrip(self):
        """状态应落进 context.yaml 并能等价重建"""
        engine = WorkflowEngine(self.tmpdir)
        engine.run()

        context_file = Path(self.tmpdir) / "shared" / "context.yaml"
        self.assertTrue(context_file.exists())

        with open(context_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        rebuilt = ContextSchemaV2.from_dict(data)

        # from_dict ∘ (yaml) ∘ to_dict 应与引擎内存态等价
        self.assertEqual(rebuilt.to_dict(), engine.context.to_dict())

    def test_recovery_from_failed_step(self):
        """中断续跑：从失败步骤恢复，不从头重跑"""
        engine = WorkflowEngine(self.tmpdir)
        # 模拟：01-04 成功、05 失败、其余 pending
        for sid in ["01_container_preparation", "02_admission",
                    "03_v3_discovery_startup", "04_v3_discovered"]:
            engine.context.steps[sid].status = "success"
        engine.context.steps["05_v3_startup_tuning"].status = "failed"
        engine.context.current_step_id = "05_v3_startup_tuning"
        engine._save_context()

        # 新引擎从磁盘加载并续跑
        engine2 = WorkflowEngine(self.tmpdir)
        self.assertEqual(engine2.detect_recovery_point(), "05_v3_startup_tuning")
        ctx = engine2.run()

        for step_id, _ in WORKFLOW_STEPS:
            self.assertEqual(ctx.steps[step_id].status, "success")
        self.assertEqual(ctx.current_step_id, "15_finalize")


class TestStateStoreValidation(unittest.TestCase):
    """StateStore 写入校验（字段权限 + 篡改防护）"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = YamlStateStore(Path(self.tmpdir) / "context.yaml")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_accept_valid_minimal(self):
        """合法最小 context（字段是 schema 子集）应通过"""
        self.store.save({"schema_version": "2.0", "steps": {}})
        self.assertEqual(self.store.load()["schema_version"], "2.0")

    def test_reject_unknown_toplevel_field(self):
        """schema 外的顶层字段应被拒绝"""
        with self.assertRaises(ContextValidationError):
            self.store.save({"schema_version": "2.0", "bogus_field": 1})

    def test_reject_wrong_schema_version(self):
        """错误 schema_version 应被拒绝"""
        with self.assertRaises(ContextValidationError):
            self.store.save({"schema_version": "1.0"})

    def test_reject_frozen_revision_tamper(self):
        """篡改已冻结 revision 应被拒绝"""
        base = {
            "schema_version": "2.0",
            "operator_revisions": {
                "r1": {"revision_id": "r1", "frozen": True, "enabled_ops": []},
            },
        }
        self.store.save(base)  # 首次写入，old=None
        tampered = {
            "schema_version": "2.0",
            "operator_revisions": {
                "r1": {"revision_id": "r1", "frozen": True, "enabled_ops": ["x"]},
            },
        }
        with self.assertRaises(ContextValidationError):
            self.store.save(tampered)

    def test_reject_success_step_regression(self):
        """已 success 的步骤回退状态应被拒绝"""
        base = {
            "schema_version": "2.0",
            "steps": {"s": {"step_id": "s", "status": "success"}},
        }
        self.store.save(base)
        regressed = {
            "schema_version": "2.0",
            "steps": {"s": {"step_id": "s", "status": "pending"}},
        }
        with self.assertRaises(ContextValidationError):
            self.store.save(regressed)


if __name__ == "__main__":
    unittest.main()
