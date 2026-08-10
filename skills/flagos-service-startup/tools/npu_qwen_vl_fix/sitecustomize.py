"""sglang native 纯净基线：qwen_vl_processor 兼容修复注入（sitecustomize）。

背景（2026-08-10 实测定位）：
  sglang 原生 ``sglang.srt.hardware_backend.npu.modules.qwen_vl_processor`` 的
  ``npu_wrapper_preprocess._preprocess`` 把 ``interpolation`` 声明为**无默认值的
  必需参数**，而 transformers 的 qwen2_vl image_processing 调用链
  （``_preprocess_image_like_inputs -> self._preprocess(images, *args, **kwargs)``）
  不传该关键字 → ``TypeError: missing 1 required positional argument: 'interpolation'``。
  Qwen3.6 系模型 config 带 vision_config → sglang 判定 VLM → 服务端 warmup
  （http_server.py 构造带最小 PNG 的请求）→ 500 → ``Initialization failed. warmup
  error`` → 启动失败。
  修复版模块由 sglang_fl 插件携带（``_qwen_vl_processor_impl.py``，wrapper 改
  ``*args/**kwargs`` 兼容），但 native 纯净基线不能加载整个插件（插件会注册
  dispatch/Communicator hooks）。本文件仅通过 importlib 加载修复模块并注入
  ``sys.modules`` 到 sglang 期望路径，早于 base_processor 的 lazy import。

启用方式：start_service.sh native 分支设 ``SGLANG_FL_QWEN_VL_FIX=1`` 并把本目录
加入 ``PYTHONPATH``（sitecustomize 在解释器启动时自动执行）。
"""
import os
import sys

if os.environ.get("SGLANG_FL_QWEN_VL_FIX") == "1":
    import importlib.util

    _IMPL = "/usr/local/python3.11.14/lib/python3.11/site-packages/sglang_fl/dispatch/backends/vendor/ascend/patches/_qwen_vl_processor_impl.py"
    _TARGET = "sglang.srt.hardware_backend.npu.modules.qwen_vl_processor"
    try:
        spec = importlib.util.spec_from_file_location("_qwen_vl_processor_impl_fix", _IMPL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[_TARGET] = mod
        print(f"[sitecustomize] qwen_vl_processor fix injected -> {_TARGET}")
    except Exception as e:  # 注入失败不致命：回退原生模块，与现状等价
        print(f"[sitecustomize] qwen_vl_processor fix inject failed: {e}", file=sys.stderr)
