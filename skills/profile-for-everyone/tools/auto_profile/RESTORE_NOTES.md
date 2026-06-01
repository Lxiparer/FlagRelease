# auto_profile — restore & alignment notes (vs Ascend v27)

This `auto_profile/` is the H100/Hygon front-end of the pipeline. It was realigned
to the Ascend **v27** package so it does not drift, except for the agreed platform
differences. `probe`, `compare`, `clean_compare_and_generate_dashboard.py` and
`probe_and_compare.py` were **not** touched.

## diyConfig.py
Restored **byte-for-byte from v27** (`diff` is empty). You get back the v27 UX:
Chinese/English language toggle, numeric menus, edit-required / edit-global /
edit-vendor / edit-gems sub-menus, arbitrary dotted-path set/clear, and
"打印最终生效配置 / write preview". It drives `profile_runner.py print-config`
using the v27 CLI (`--upstream / --config / --override / --target both`).

## profile_runner.py
Rebuilt from the v27 runner (the previous copy was broken: duplicate function
defs and call-sites whose argument order/signatures did not match, so even
`print-config` raised `TypeError: unhashable type: 'dict'`). Kept identical to
v27: config schema, deep-merge override semantics (`override.json` is a full null
template, blanks are pruned), container reuse/lifecycle, vLLM command rewriting
(`--host/--port/--served-model-name/model_path` forced, `--profiler-config`
injected), warmup/ready logic, and the **v27 CLI** (`--upstream/--config/--override/--target`).

**H100/Hygon-only differences (the agreed platform deltas):**
- No `analyse.py` / `torch_npu` post-processing — the torch profiler writes
  `*.pt.trace.json.gz` straight into `./vllm_profile`.
- `stop_profile` can take >1h. We POST `/stop_profile` with a long timeout
  (`profile.trace_wait_timeout_s`, default 7200s), then poll the container until
  the `json.gz` sizes stop changing — size-stable == flush finished. vLLM is only
  Ctrl+C'd **after** the trace is safely copied out.
- rank0 selection: prefers `rank0` / `rank-0` / `rank_0` (and a `dp0-ep0-pp0`
  prefix), falls back to the single/first `json.gz`. `rank10`/`rank1` are not
  mistaken for rank0.
- The host artifact is `docker cp`'d and renamed to `<target>.pt.trace.json.gz`
  under `host_output_dir` (default `./traceSource_both`, vs v27's `../DBSource_both`).
- Devices (`--gpus all` for NVIDIA, Hygon device flags) live in `*_docker_run.sh`.

New CLI nicety vs v27: `run --dry-run` prints the effective config without
touching Docker (used by `run_full_pipeline.py`).

## *.sh files
Aligned to the v27 convention: each file holds a **real** command and the runner
rewrites it — no `{{PLACEHOLDER}}` substitution. `*_docker_run.sh` carries a real
`docker run ... --name <x> ... <image> sleep infinity` (the runner patches
`--name`); `*_vllm_serve.sh` carries a real `vllm serve <model> ...` (the runner
forces host/port/served-model-name/model_path and injects `--profiler-config`).
NVIDIA `--gpus all` is the default; adjust device flags for Hygon.

## override.json
Full null template, derived from v27's, with the Ascend-only `analyse`/`analyse_s`
removed and `output_db_name` replaced by `trace_output_name` plus
`trace_wait_timeout_s` / `trace_poll_interval_s`.

## One nuance to know
diyConfig is verbatim v27, so its "编辑 gems/vendor 可选覆盖项 → 8. profile.output_db_name"
still exists. On H100/Hygon the runner ignores `output_db_name` (there is no `.db`);
the trace name is derived as `<target>.pt.trace.json.gz`. If you ever need to rename
the host artifact, set `profile.trace_output_name` (e.g. via diyConfig menu item 5,
"set arbitrary override path").
