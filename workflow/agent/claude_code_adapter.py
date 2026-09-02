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

"""Claude Code Adapter - 适配 Claude Code 为 Analysis Agent

用法：
    python3 claude_code_adapter.py --request <request_id>

工作流程：
1. 读取 /flagos-workspace/agent_requests/<request_id>.json
2. 提取 prompt 和约束
3. 调用 Claude Code（通过 subprocess 或 API）
4. 解析 Claude Code 输出为结构化 JSON
5. 写入 /flagos-workspace/agent_results/<request_id>.json
"""

import os
import sys
import json
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
import logging

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("claude_code_adapter")


def load_request(request_id: str, workspace_root: Path) -> dict:
    """加载 request 文件

    Args:
        request_id: Request ID
        workspace_root: Workspace 根目录

    Returns:
        Request 数据
    """
    request_file = workspace_root / "agent_requests" / f"{request_id}.json"

    if not request_file.exists():
        raise FileNotFoundError(f"Request file not found: {request_file}")

    with open(request_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def invoke_claude_code_api(prompt: str, schema: dict, timeout: int = 900) -> dict:
    """通过 Anthropic API 调用 Claude（结构化输出）

    Args:
        prompt: 分析 prompt
        schema: JSON schema
        timeout: 超时时间

    Returns:
        结构化 JSON 结果
    """
    # 这里需要实际的 Anthropic API 调用
    # 简化实现：假设已有 ANTHROPIC_API_KEY

    import anthropic

    client = anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
    )

    try:
        message = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=4096,
            temperature=0.0,
            system="You are a FlagOS diagnostic expert. Analyze the provided evidence and return structured JSON output.",
            messages=[
                {"role": "user", "content": prompt}
            ],
            # 结构化输出（如果 SDK 支持）
            # response_format={"type": "json_object", "schema": schema}
        )

        # 提取 JSON（可能需要解析 text）
        response_text = message.content[0].text

        # 尝试解析 JSON
        if "```json" in response_text:
            json_start = response_text.find("```json") + 7
            json_end = response_text.find("```", json_start)
            json_text = response_text[json_start:json_end].strip()
        else:
            json_text = response_text.strip()

        result = json.loads(json_text)
        return result

    except Exception as e:
        logger.error(f"Claude API call failed: {e}")
        return {
            "schema_version": "1.0",
            "agent_session_id": f"as-error-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "status": "error",
            "error_message": str(e),
            "error_type": "api_failure",
        }


def invoke_claude_code_cli(prompt: str, workspace_root: Path, timeout: int = 900) -> dict:
    """通过 Claude Code CLI 调用（备选方案）

    Args:
        prompt: 分析 prompt
        workspace_root: Workspace 根目录
        timeout: 超时时间

    Returns:
        结构化 JSON 结果
    """
    # 将 prompt 写入临时文件
    prompt_file = workspace_root / "agent_requests" / "_current_prompt.txt"
    with open(prompt_file, 'w', encoding='utf-8') as f:
        f.write(prompt)

    # 调用 claude CLI（假设已安装）
    try:
        result = subprocess.run(
            [
                "claude",
                "-p", prompt,
                "--output-format", "text",
            ],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI failed: {result.stderr}")

        output = result.stdout

        # 解析 JSON
        if "```json" in output:
            json_start = output.find("```json") + 7
            json_end = output.find("```", json_start)
            json_text = output[json_start:json_end].strip()
        else:
            json_text = output.strip()

        return json.loads(json_text)

    except subprocess.TimeoutExpired:
        return {
            "schema_version": "1.0",
            "status": "error",
            "error_message": f"Timeout after {timeout}s",
            "error_type": "timeout",
        }
    except Exception as e:
        logger.error(f"Claude CLI call failed: {e}")
        return {
            "schema_version": "1.0",
            "status": "error",
            "error_message": str(e),
            "error_type": "cli_failure",
        }


def save_result(request_id: str, result: dict, workspace_root: Path):
    """保存结果到文件

    Args:
        request_id: Request ID
        result: 结果字典
        workspace_root: Workspace 根目录
    """
    result_file = workspace_root / "agent_results" / f"{request_id}.json"
    result_file.parent.mkdir(parents=True, exist_ok=True)

    # 添加元数据
    result["_meta"] = result.get("_meta", {})
    result["_meta"]["adapter_version"] = "1.0"
    result["_meta"]["completed_at"] = datetime.now().isoformat()

    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    logger.info(f"Saved result to {result_file}")


def main():
    parser = argparse.ArgumentParser(description="Claude Code Adapter for Analysis Agent")
    parser.add_argument("--request", required=True, help="Request ID")
    parser.add_argument(
        "--workspace",
        default="/flagos-workspace",
        help="Workspace root directory",
    )
    parser.add_argument(
        "--method",
        choices=["api", "cli"],
        default="api",
        help="Invocation method (api or cli)",
    )
    parser.add_argument("--timeout", type=int, default=900, help="Timeout in seconds")

    args = parser.parse_args()

    workspace_root = Path(args.workspace)

    try:
        # 加载 request
        logger.info(f"Loading request: {args.request}")
        request_data = load_request(args.request, workspace_root)

        prompt = request_data["prompt"]
        analysis_type = request_data["analysis_type"]

        # 定义 JSON schema（根据 analysis_type）
        schema = {
            "type": "object",
            "properties": {
                "schema_version": {"type": "string"},
                "agent_session_id": {"type": "string"},
                "workflow_run_id": {"type": "string"},
                "operator_revision": {"type": "string"},
                "status": {"type": "string", "enum": ["hypothesis_available", "no_hypothesis", "unresolved", "error"]},
                "suspected_ops": {"type": "array"},
                "recommended_experiment": {"type": "object"},
            },
            "required": ["schema_version", "status"],
        }

        # 调用 Claude Code
        logger.info(f"Invoking Claude Code via {args.method}")
        if args.method == "api":
            result = invoke_claude_code_api(prompt, schema, args.timeout)
        else:
            result = invoke_claude_code_cli(prompt, workspace_root, args.timeout)

        # 保存结果
        save_result(args.request, result, workspace_root)

        logger.info(f"Analysis completed with status: {result.get('status')}")

        # 返回 0 表示成功
        sys.exit(0)

    except Exception as e:
        logger.error(f"Adapter failed: {e}")

        # 保存错误结果
        error_result = {
            "schema_version": "1.0",
            "status": "error",
            "error_message": str(e),
            "error_type": "adapter_error",
        }
        save_result(args.request, error_result, workspace_root)

        sys.exit(1)


if __name__ == "__main__":
    main()
