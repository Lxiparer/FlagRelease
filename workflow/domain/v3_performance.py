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

"""V3 Performance Measurement - V3 性能纯测量

设计原则（plugin-only 工作流）：
- V3 性能只测量、只记录绝对值，**不做对比、不算 ratio、不设 Gate**
- 无本地 V1 基线，外部 NV 精度是唯一业务红线
- 性能数据作为 Artifact 落盘，供 V4 优化阶段消费（V4 的对比基准是 V3）
- 性能不达标不阻断流程，仅作为发布报告参考

与旧工作流的区别：
- 移除 performance_compare.py 的 ratio 计算
- 移除 performance_ok Gate 判定
- 只保留 benchmark_runner.py 的原始测量结果
"""

import json
import logging
import os
from typing import Dict, List, Optional, Tuple

from ..schemas.context_v2 import OperatorRevision
from ..artifacts.registry import ArtifactRegistry


class V3PerformanceMeasurement:
    """V3 性能纯测量（无对比、无 Gate）"""

    def __init__(
        self,
        workspace_root: str = "/flagos-workspace",
        container_name: str = "",
        artifact_registry: Optional[ArtifactRegistry] = None,
    ):
        self.workspace_root = workspace_root
        self.container_name = container_name
        self.artifact_registry = artifact_registry or ArtifactRegistry(workspace_root)
        self.logger = logging.getLogger("workflow.domain.v3_performance")

    def measure_performance(
        self,
        candidate: str,
        revision: OperatorRevision,
        mode: str = "quick",
    ) -> Tuple[bool, Dict]:
        """测量 V3 性能（纯测量，只记录绝对值）

        Args:
            candidate: 版本标识（v3）
            revision: 当前算子 revision
            mode: benchmark 模式（quick / comprehensive）

        Returns:
            (是否测量成功, 性能结果字典)

        Note:
            无论性能高低都返回 success=True（只要测量本身成功）。
            性能不达标不是失败——V3 无本地基线可比，不设性能 Gate。
        """
        self.logger.info(
            f"Measuring V3 performance (candidate={candidate}, "
            f"mode={mode}, revision={revision.revision_id})"
        )

        # 执行 benchmark（output-name 标准命名 flagos_optimized）
        success, perf_data = self._run_benchmark(
            output_name="flagos_optimized",
            mode=mode,
        )

        if not success:
            self.logger.error("Benchmark execution failed")
            return False, {}

        # 记录绝对值（不做任何对比）
        self.logger.info(
            f"V3 performance measured: "
            f"throughput={perf_data.get('throughput_tokens_per_sec', 0):.1f} tokens/s, "
            f"TTFT={perf_data.get('ttft_ms', 0):.1f} ms"
        )

        # 落盘为 Artifact，供 V4 消费
        artifact_id = self.register_performance_artifact(
            candidate,
            revision,
            perf_data,
            mode,
        )
        perf_data["artifact_id"] = artifact_id

        return True, perf_data

    def _run_benchmark(
        self,
        output_name: str,
        mode: str,
    ) -> Tuple[bool, Dict]:
        """执行 benchmark_runner.py（唯一性能测量入口）

        Args:
            output_name: 输出命名（flagos_optimized for V3）
            mode: quick / comprehensive

        Returns:
            (是否成功, 性能数据)
        """
        cmd = (
            f"PATH=/opt/conda/bin:$PATH python3 "
            f"{self.workspace_root}/scripts/benchmark_runner.py "
            f"--mode {mode} --output-name {output_name}"
        )

        # 实际通过 docker exec 执行（此处为占位，由 Engine 注入执行器）
        result_file = os.path.join(
            self.workspace_root, "results", f"{output_name}.json"
        )

        try:
            if os.path.exists(result_file):
                with open(result_file, "r") as f:
                    perf_data = json.load(f)
                return True, perf_data
            else:
                self.logger.warning(f"Result file not found: {result_file}")
                return False, {}
        except (json.JSONDecodeError, IOError) as e:
            self.logger.error(f"Failed to read benchmark result: {e}")
            return False, {}

    def register_performance_artifact(
        self,
        candidate: str,
        revision: OperatorRevision,
        perf_data: Dict,
        mode: str,
    ) -> str:
        """注册性能结果 Artifact

        Args:
            candidate: 版本标识
            revision: 算子 revision
            perf_data: 性能数据
            mode: benchmark 模式

        Returns:
            artifact_id
        """
        # 纯测量内容：只记录绝对值，无 ratio / baseline / qualified 字段
        content = {
            "candidate": candidate,
            "operator_revision": revision.revision_id,
            "test_type": mode,
            "throughput_tokens_per_sec": perf_data.get("throughput_tokens_per_sec", 0.0),
            "ttft_ms": perf_data.get("ttft_ms", 0.0),
            "tpot_ms": perf_data.get("tpot_ms", 0.0),
            "concurrency_results": perf_data.get("concurrency_results", []),
            "input_length": perf_data.get("input_length", 4096),
            "output_length": perf_data.get("output_length", 1024),
            "_meta": {"measurement_only": "true"},
        }

        file_path = os.path.join("results", "flagos_optimized.json")

        artifact_id = self.artifact_registry.register_artifact(
            artifact_type="performance-result",
            content=content,
            file_path=file_path,
            generated_by="script",
            generator_version="v3_performance_measurement_1.0",
            tags={
                "candidate": candidate,
                "operator_revision": revision.revision_id,
                "measurement_only": "true",
            },
        )
        self.logger.info(f"Registered performance artifact: {artifact_id}")
        return artifact_id
