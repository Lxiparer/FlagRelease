# agent/ — Claude Code triage layer

Turns a finished probe+compare run into a self-contained bundle that Claude Code can use
to find out why **Gems** is slower than **Vendor** for a given semantic, locate the
operator source via the CPU call stack, and propose/verify a fix.

## What runs

`generate_skills.py` (called by `run_full_pipeline.py` after probe+compare, or by hand):

```bash
python agent/generate_skills.py \
  --run-dir ./output \
  --profile-dir ./auto_profile \
  --decode-idx 30 \
  [--container-context-json ctx.json] \
  [--source-path-map /vllm-workspace=/abs/local]
```

It produces, inside `--run-dir`:

- `agent_triage_queue.jsonl` + `agent_semantic_index.json` — built by
  `semantic_perf_query.build_index` (Gems-slower targets, sorted by absolute
  kernel-time delta).
- `agent_container_context.json` — built from the auto_profile config via
  `profile_context.build_profile_context` (per-side container name, workdir, image,
  model_path…). This is what lets the agent enter the right container. Previously
  nothing generated this; `generate_skills.py` now does (unless you pass an explicit
  `--container-context-json`).
- `claude_skills/gpu-semantic-triage/` — a copy of the repo skill (SKILL.md +
  references/OUTPUT_SCHEMA.md + scripts/), so the bundle is portable.
- `CLAUDE_TRIAGE.md` — the entry point. Embeds the sorted Gems-slower target table, the
  run artifacts, the recommended `semantic_perf_query.py` commands, and per-side
  "enter the container" instructions.

## The skill: claude_skills/gpu-semantic-triage/

Mirrors v27's `ascend-semantic-triage`, reworded for H100/Hygon and pointed at this
pipeline's outputs (`src/gems`/`src/vendor`, `compare_dashboard.html`, trace slices).
Three scripts back it:

- `semantic_perf_query.py` — read-only query layer: `build-index`, `list-targets`,
  `describe`, `kernels`, `stacks`, `source`. Reads the two
  `*.cpu_stack.normalized_tree.json` trees and imports
  `clean_compare_and_generate_dashboard.py` for the compare payload. No `.db`.
- `container_source_query.py` — controlled, read-only container source access
  (`show-context`, `snapshot`, `source`, `verify`). Refuses to start both containers;
  `verify` needs `--yes`.
- `profile_context.py` — builds `agent_container_context.json` from the auto_profile
  config. Uses the runner's `build_effective_config_bundle(upstream, local, "both",
  override)`, so the agent sees the same defaults as `auto_probe`.

These three are v27 code with only small, reviewed H100 adaptations (input paths,
compare-module name, `triton`/`flaggems`/`cuda::`/`hip::` source patterns). They are the
analysis source — Claude Code must NOT analyze from the HTML.

## Note

`profile_context.py` is byte-identical to v27, so the context JSON still carries the
label `kind: "ascend_profile_runner_container_context"` and a vestigial
`output_db_name: null` per side. Both are cosmetic on H100/Hygon — `container_source_query.py`
keys off the `sides` map, not the label, and the trace artifact is the renamed json.gz.
