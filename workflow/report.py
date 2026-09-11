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

"""Workflow Report - 新 schema 报告生成（plan §9 / 工作段 10 的最小可用实现）

数据来源只有两处：`ContextSchemaV2` 与 `ArtifactRegistry`——不读任何旧文件名、
不按 Agent 自然语言推断状态。**不产出 V1/V2 性能比**（新流程没有本地 V1），
V3 性能只展示绝对值。

产出：
- `<workspace>/results/report.md`   人读
- `<workspace>/results/report.json` 机读（batch/通知消费）
- `<workspace>/shared/context_final.yaml` 全流程结束时的状态快照
- `<workspace>/artifacts/index.json` Artifact 索引（含校验状态）

与旧 `shared/generate_report.py`（读旧 schema）迁移期并存、互不覆盖。
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from .artifacts.artifact_schema import compute_artifact_hash
from .schemas.context_v2 import ContextSchemaV2

logger = logging.getLogger("workflow.report")

REPORT_MD = ("results", "report.md")
REPORT_JSON = ("results", "report.json")
# 与 CLAUDE.md 的既有约定一致（全流程结束时回传的最终状态快照）
CONTEXT_FINAL = ("shared", "context_final.yaml")
# Artifact 索引落在 registry 同目录（引擎模式下即 config/engine/artifacts/，每轮随归档清理）
ARTIFACT_INDEX_NAME = "index.json"

# 校验状态
VERIFIED = "verified"            # 文件存在且哈希匹配
HASH_MISMATCH = "hash_mismatch"  # 文件存在但哈希不符 → 篡改/损坏（唯一告警态）
MISSING_FILE = "missing_file"    # 内容已在 registry 记录，但落盘文件缺失
NO_FILE = "no_file"              # 该 artifact 本就不对应文件（content_size=0）

# V4 发布结果 artifact 的两种落点
V4_RELEASE_ARTIFACT = "v4-release-decision"


def _rel(workspace_root: Path, parts: Tuple[str, ...]) -> Path:
    return workspace_root.joinpath(*parts)


# ----------------------------------------------------------------------
# Artifact 视图
# ----------------------------------------------------------------------

def artifact_index(registry) -> List[Dict]:
    """Artifact 索引 + 逐条校验状态（哈希不符是最需要暴露的异常）"""
    index = []
    for artifact_id, entry in sorted(registry.artifacts.items()):
        meta = entry.get("metadata", {})
        rel_path = meta.get("file_path", "")
        full = Path(registry.workspace_root) / rel_path if rel_path else None

        if not rel_path or not full or not full.exists():
            state = MISSING_FILE if meta.get("content_size") else NO_FILE
        else:
            try:
                with open(full, "r", encoding="utf-8") as f:
                    content = json.load(f)
                state = (VERIFIED if compute_artifact_hash(content) == meta.get("content_hash")
                         else HASH_MISMATCH)
            except (json.JSONDecodeError, OSError):
                state = HASH_MISMATCH

        index.append({
            "artifact_id": artifact_id,
            "artifact_type": meta.get("artifact_type", ""),
            "file_path": rel_path,
            "content_hash": meta.get("content_hash", ""),
            "created_at": meta.get("created_at", ""),
            "tags": meta.get("tags", {}),
            "verify_status": state,
        })
    return index


def _latest_content(registry, artifact_type: str) -> Optional[Dict]:
    """取最新一条指定类型 artifact 的内容（落盘文件优先，缺失时用 registry 摘要）"""
    art_id = registry.get_latest_artifact(artifact_type)
    if not art_id:
        return None
    content = registry.load_artifact_content(art_id)
    if isinstance(content, dict):
        return content
    entry = registry.get_artifact(art_id) or {}
    summary = entry.get("content_summary")
    return summary if isinstance(summary, dict) else None


def _v3_performance(context: ContextSchemaV2, registry) -> Optional[Dict]:
    """V3 性能（纯测量绝对值，**不做任何比值**）"""
    content = _latest_content(registry, "performance-result")
    if not content:
        return None
    # performance-result 的 tag 里有 candidate；取 candidate=v3 的最新一条
    art_id = registry.get_latest_artifact("performance-result", tags={"candidate": "v3"})
    if art_id:
        content = registry.load_artifact_content(art_id) or content
    return {
        "throughput_tokens_per_sec": content.get("throughput_tokens_per_sec"),
        "ttft_ms": content.get("ttft_ms"),
        "tpot_ms": content.get("tpot_ms"),
        "test_type": content.get("test_type", ""),
        "operator_revision": content.get("operator_revision", ""),
        "_note": "纯测量绝对值；新流程无本地 V1 基线，不产出性能比",
    }


# ----------------------------------------------------------------------
# 报告构建
# ----------------------------------------------------------------------

def build_report(
    context: ContextSchemaV2,
    registry,
    workspace_root: Path,
) -> Dict:
    """从 Context + Artifact Registry 构建报告数据（唯一事实来源）"""
    run = context.runtime
    steps = [
        {
            "step_id": s.step_id,
            "step_name": s.step_name,
            "status": s.status,
            "started_at": s.started_at,
            "finished_at": s.finished_at,
            "duration_seconds": s.duration_seconds,
            "output_artifacts": list(s.output_artifacts),
            "fail_reason": s.fail_reason,
            "skip_reason": s.skip_reason,
        }
        for s in context.steps.values()
    ]

    gates = [
        {
            "gate_id": g.gate_id,
            "status": g.status,
            "criteria": g.criteria,
            "reason": g.reason,
            "evaluated_at": g.evaluated_at,
        }
        for g in context.gates.values()
    ]

    revisions = [
        {
            "revision_id": r.revision_id,
            "parent_revision_id": r.parent_revision_id,
            "enabled_count": len(r.enabled_ops),
            "disabled_count": len(r.disabled_ops),
            "frozen": r.frozen,
            "disable_reason_categories": r.disable_reason_categories,
        }
        for r in context.operator_revisions.values()
    ]

    # establishment：只认 Gate + 冻结 revision 两个事实
    v3_final = context.operator_revisions.get("v3-final")
    v4_final = context.operator_revisions.get("v4-final")
    v3_acc = context.gates.get("accuracy.v3.qualified")
    v4_est = context.gates.get("v4.established")
    establishment = {
        "v3": {
            "established": bool(v3_final and v3_final.frozen
                                and v3_acc and v3_acc.status == "passed"),
            "final_revision_frozen": bool(v3_final and v3_final.frozen),
            "accuracy_gate": v3_acc.status if v3_acc else "missing",
        },
        "v4": {
            "established": bool(v4_est and v4_est.status == "passed"
                                and v4_final and v4_final.frozen),
            "gate": v4_est.status if v4_est else "missing",
            "gate_reason": v4_est.reason if v4_est else "",
            "fallback_to_v3": bool(not (v4_est and v4_est.status == "passed")),
        },
    }

    # release：直接取发布决策 artifact（引擎在步骤10/13 落盘）
    v3_release = _latest_content(registry, "release-decision")
    v4_release = _latest_content(registry, V4_RELEASE_ARTIFACT)
    release = {
        "v3": _release_view(v3_release),
        "v4": _release_view(v4_release),
    }

    index = artifact_index(registry)
    artifacts_summary = {
        "total": len(index),
        "verified": sum(1 for a in index if a["verify_status"] == VERIFIED),
        "hash_mismatch": sum(1 for a in index if a["verify_status"] == HASH_MISMATCH),
        "missing_file": sum(1 for a in index if a["verify_status"] == MISSING_FILE),
        "no_file": sum(1 for a in index if a["verify_status"] == NO_FILE),
    }

    return {
        "schema_version": context.schema_version,
        "generated_at": datetime.now().isoformat(),
        "run": {
            "workflow_run_id": run.workflow_run_id,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "model_name": run.model_name,
            "container_name": run.container_name,
            "entry_image_type": run.entry_image_type,
            "current_step_id": context.current_step_id,
            "current_revision_id": context.current_revision_id,
        },
        "steps": steps,
        "gates": gates,
        "operator_revisions": revisions,
        "establishment": establishment,
        "release": release,
        "v3_performance": _v3_performance(context, registry),
        "artifacts_summary": artifacts_summary,
        "artifacts": index,
        "_meta": {
            "source": "ContextSchemaV2 + ArtifactRegistry（不读旧文件名、不推断 Agent 文本）",
            "note": "新流程无本地 V1/V2，报告不含性能比；性能只展示 V3 绝对值",
        },
    }


def _release_view(content: Optional[Dict]) -> Optional[Dict]:
    """归一化发布决策视图（V3/V4 的 report 字段名不同，取交集 + 原样附全量）"""
    if not content:
        return None
    return {
        "version": content.get("version", ""),
        "revision_id": content.get("revision_id", ""),
        "release_scope": content.get("release_scope", ""),
        "image_tag": content.get("image_tag", ""),
        "published_to": (content.get("artifacts") or {}).get("published_to", []),
        "fallback_to_v3": content.get("fallback_to_v3", False),
        "reason": content.get("reason", ""),
        "success": content.get("success"),
    }


# ----------------------------------------------------------------------
# 渲染与落盘
# ----------------------------------------------------------------------

def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def render_markdown(report: Dict) -> str:
    """渲染人读报告"""
    run = report["run"]
    est = report["establishment"]
    lines: List[str] = []

    lines.append(f"# FlagOS 迁移报告 · {run['model_name'] or '（未知模型）'}")
    lines.append("")
    lines.append(f"- 运行 ID：`{run['workflow_run_id']}`")
    lines.append(f"- 容器：`{run['container_name']}` ｜ 准入镜像类型：`{run['entry_image_type']}`")
    lines.append(f"- 起止：{_fmt(run['started_at'])} → {_fmt(run['finished_at'])}")
    lines.append(f"- 当前步骤：`{run['current_step_id']}` ｜ 当前 revision：`{run['current_revision_id']}`")
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append("")
    lines.append("> 本报告由确定性引擎从 Context + Artifact 生成；"
                 "新流程没有本地基线版本，性能只展示绝对值、不产出任何版本间比值。")
    lines.append("")

    # 版本成立
    lines.append("## 版本成立")
    lines.append("")
    lines.append("| 版本 | 成立 | 依据 |")
    lines.append("|---|---|---|")
    lines.append(
        f"| V3 | {'✅' if est['v3']['established'] else '❌'} | "
        f"v3-final 冻结={est['v3']['final_revision_frozen']}，"
        f"精度 Gate={est['v3']['accuracy_gate']} |"
    )
    v4 = est["v4"]
    lines.append(
        f"| V4 | {'✅' if v4['established'] else '❌'} | "
        f"Gate={v4['gate']}"
        + (f"，回退 V3：{v4['gate_reason']}" if v4["fallback_to_v3"] else "")
        + " |"
    )
    lines.append("")

    # 发布
    lines.append("## 发布")
    lines.append("")
    lines.append("| 版本 | 范围 | 镜像 tag | 目的地 |")
    lines.append("|---|---|---|---|")
    for ver in ("v3", "v4"):
        rel = report["release"].get(ver)
        if not rel:
            lines.append(f"| {ver.upper()} | 未发布 | — | — |")
            continue
        dest = "、".join(rel["published_to"]) or ("回退 V3（未发布 V4）" if rel["fallback_to_v3"] else "—")
        lines.append(
            f"| {ver.upper()} | {rel['release_scope'] or '—'} | "
            f"`{rel['image_tag'] or '—'}` | {dest} |"
        )
    lines.append("")

    # 性能（绝对值）
    lines.append("## V3 性能（纯测量，绝对值）")
    lines.append("")
    perf = report.get("v3_performance")
    if perf:
        lines.append(f"- 吞吐：**{_fmt(perf['throughput_tokens_per_sec'])} tokens/s**")
        lines.append(f"- TTFT：{_fmt(perf['ttft_ms'])} ms ｜ TPOT：{_fmt(perf['tpot_ms'])} ms")
        lines.append(f"- 模式：{perf['test_type'] or '—'} ｜ 算子集：`{perf['operator_revision'] or '—'}`")
        lines.append("- 说明：性能不设 Gate、不阻断流程，仅作为发布参考。")
    else:
        lines.append("- 未测得有效性能结果。")
    lines.append("")

    # Gate
    lines.append("## Gate")
    lines.append("")
    lines.append("| Gate | 状态 | 原因 |")
    lines.append("|---|---|---|")
    for g in sorted(report["gates"], key=lambda x: x["gate_id"]):
        mark = {"passed": "✅", "failed": "❌", "pending": "…", "unresolved": "⚠"}.get(g["status"], "?")
        lines.append(f"| `{g['gate_id']}` | {mark} {g['status']} | {g['reason'][:100]} |")
    lines.append("")

    # 步骤
    lines.append("## 步骤执行")
    lines.append("")
    lines.append("| 步骤 | 状态 | 耗时(s) | 产出 Artifact | 备注 |")
    lines.append("|---|---|---|---|---|")
    for s in report["steps"]:
        mark = {"success": "✅", "skipped": "⏭", "failed": "❌", "running": "⏳"}.get(s["status"], "…")
        note = s["fail_reason"] or s["skip_reason"] or ""
        lines.append(
            f"| `{s['step_id']}` | {mark} {s['status']} | {_fmt(s['duration_seconds'])} | "
            f"{len(s['output_artifacts'])} | {note[:80]} |"
        )
    lines.append("")

    # 算子 revision
    lines.append("## 算子 revision")
    lines.append("")
    lines.append("| revision | 启用 | 禁用 | 冻结 | 禁用分类 |")
    lines.append("|---|---|---|---|---|")
    for r in report["operator_revisions"]:
        cats = "，".join(f"{k}={len(v)}" for k, v in (r["disable_reason_categories"] or {}).items() if v)
        lines.append(
            f"| `{r['revision_id']}` | {r['enabled_count']} | {r['disabled_count']} | "
            f"{'✅' if r['frozen'] else '—'} | {cats or '—'} |"
        )
    lines.append("")

    # Artifact 校验
    a = report["artifacts_summary"]
    lines.append("## Artifact")
    lines.append("")
    lines.append(
        f"- 共 **{a['total']}** 条：校验通过 {a['verified']} ｜ 摘要态 {a['no_file']} ｜ "
        f"缺文件 {a['missing_file']} ｜ **哈希不符 {a['hash_mismatch']}**"
    )
    if a["hash_mismatch"]:
        lines.append("- ⚠ 存在哈希不符的 Artifact（内容被改动或损坏），下列条目不被视为有效证据：")
        for art in report["artifacts"]:
            if art["verify_status"] == HASH_MISMATCH:
                lines.append(f"  - `{art['artifact_id']}` ({art['artifact_type']}) {art['file_path']}")
    lines.append("")

    return "\n".join(lines) + "\n"


def write_report(workspace_root: Path, context: ContextSchemaV2, registry) -> Tuple[str, str]:
    """生成并落盘 report.md / report.json，返回 (md 路径, json 路径)"""
    report = build_report(context, registry, workspace_root)

    md_path = _rel(workspace_root, REPORT_MD)
    json_path = _rel(workspace_root, REPORT_JSON)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    logger.info(f"Report written: {md_path} / {json_path}")
    return str(md_path), str(json_path)


def write_context_final(workspace_root: Path, context: ContextSchemaV2) -> str:
    """回传全流程最终状态快照 context_final.yaml"""
    path = _rel(workspace_root, CONTEXT_FINAL)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(context.to_dict(), f, allow_unicode=True, sort_keys=False)
    logger.info(f"context_final written: {path}")
    return str(path)


def write_artifact_index(workspace_root: Path, registry) -> str:
    """落盘 Artifact 索引（含校验状态；落在 registry 同目录）"""
    registry_root = Path(getattr(registry, "registry_root", workspace_root))
    path = registry_root / ARTIFACT_INDEX_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    index = artifact_index(registry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "count": len(index),
            "artifacts": index,
        }, f, ensure_ascii=False, indent=2)
    logger.info(f"Artifact index written: {path}")
    return str(path)
