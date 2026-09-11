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

"""确定性建容器：厂商模板数据化 + fail-closed

为什么要它：容器创建原先只在 prompt 里、由 agent 执行。引擎模式要"不依赖 agent"，
就必须有确定性建容器路径；否则只能去复用上一次的容器——而 SKILL.md 明令
「镜像模式下禁止复用任何已存在的容器（复用旧容器=旧镜像跑新任务，产出错误归属）」。
"""

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_TOOL = (Path(__file__).parent.parent.parent
         / "skills" / "flagos-container-preparation" / "tools" / "create_container.py")
_spec = importlib.util.spec_from_file_location("create_container", _TOOL)
create_container = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(create_container)


class TestTemplates(unittest.TestCase):
    def setUp(self):
        self.templates = create_container.load_templates()

    def test_all_vendors_present(self):
        """厂商模板齐备（加厂商=加数据；缺哪个一眼可见）"""
        self.assertEqual(
            set(self.templates),
            {"zhenwu", "nvidia", "ascend", "mthreads", "metax", "cambricon", "hygon"},
        )

    def test_every_template_expands_cleanly(self):
        """模板展开后不留未替换的占位符"""
        values = {"container": "c", "model_path": "/mp", "container_model_path": "/mp",
                  "workspace": "/ws", "image": "img", "shm": "64g"}
        for vendor, tpl in self.templates.items():
            argv = create_container.build_argv(vendor, tpl, values)
            self.assertEqual(argv[:2], ["docker", "run"])
            for token in argv:
                self.assertNotIn("{", token, f"{vendor} 模板有未替换占位符：{token}")
            self.assertIn("img", argv, f"{vendor} 模板缺镜像")

    def test_nvidia_template_has_no_extra_params(self):
        """NVIDIA 模板严禁添加模板外参数（--privileged/--ipc=host/--shm-size 会触发 authZ 拒绝）"""
        argv = create_container.build_argv(
            "nvidia", self.templates["nvidia"],
            {"container": "c", "model_path": "/mp", "container_model_path": "/mp",
             "workspace": "/ws", "image": "img", "shm": "64g"})
        self.assertIn("--gpus=all", argv)
        for forbidden in ("--privileged", "--ipc=host", "--shm-size", "--ulimit"):
            self.assertNotIn(forbidden, argv, f"NVIDIA 模板不得含 {forbidden}")

    def test_non_nvidia_templates_do_not_use_gpus_all(self):
        """--gpus=all 是 NVIDIA 专属（PPU/昇腾/寒武纪等会失败）"""
        for vendor, tpl in self.templates.items():
            if vendor == "nvidia":
                continue
            argv = create_container.build_argv(
                vendor, tpl,
                {"container": "c", "model_path": "/mp", "container_model_path": "/mp",
                 "workspace": "/ws", "image": "img", "shm": "64g"})
            self.assertNotIn("--gpus=all", argv, f"{vendor} 不得用 --gpus=all")


class TestCreateContainerCLI(unittest.TestCase):
    """命令行行为：dry-run 展开 / 不支持厂商 fail-closed / 拒绝复用已存在容器"""

    def _main(self, argv):
        old = sys.argv
        sys.argv = ["create_container.py"] + argv
        try:
            return create_container.main()
        finally:
            sys.argv = old

    def test_dry_run_expands_template(self):
        rc = self._main([
            "--image", "img:v1", "--model-name", "Qwen/Qwen3-8B",
            "--container-name", "Qwen3-8B_flagos", "--model-path", "/data/models/Qwen3-8B",
            "--vendor", "nvidia", "--dry-run",
        ])
        self.assertEqual(rc, 0)

    def test_unsupported_vendor_fails_closed(self):
        """没有模板的厂商 → 退 2（不猜、不复用），并说明已支持哪些"""
        rc = self._main([
            "--image", "img", "--model-name", "M", "--container-name", "C",
            "--model-path", "/p", "--vendor", "some_new_vendor", "--dry-run",
        ])
        self.assertEqual(rc, 2)

    def test_existing_container_is_refused(self):
        """镜像模式下容器已存在 → 拒绝（禁止复用，由编排层改名）"""
        orig = create_container.container_exists
        create_container.container_exists = lambda name: True
        try:
            rc = self._main([
                "--image", "img", "--model-name", "M", "--container-name", "already_there",
                "--model-path", "/p", "--vendor", "nvidia", "--dry-run",
            ])
        finally:
            create_container.container_exists = orig
        self.assertEqual(rc, 2, "复用已存在容器必须被拒绝")

    def test_vendor_detection_failure_fails_closed(self):
        """厂商探测失败 → 退 2（不在"不知道什么卡"的情况下猜模板）"""
        orig = create_container.detect_vendor
        create_container.detect_vendor = lambda explicit="": ("", "测试：探测失败")
        try:
            rc = self._main([
                "--image", "img", "--model-name", "M", "--container-name", "C",
                "--model-path", "/p", "--dry-run",
            ])
        finally:
            create_container.detect_vendor = orig
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
