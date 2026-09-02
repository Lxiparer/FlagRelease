# Plugin-only Workflow Implementation Status

**Last Updated**: 2026-09-02  
**Branch**: `workflow-refactor`

---

## Overview

This document tracks the implementation progress of the Plugin-only workflow refactor, which consolidates the dual pipeline architecture (Branch A/B) into a single, deterministic workflow based on vllm + flaggems + flagtree + vllm-plugin-FL.

---

## Implementation Plan (15 Steps)

### Phase 1: Domain Model & Core Infrastructure (Steps 1-4) ✅

- [x] **Step 1**: Artifact Registry with content hashing
- [x] **Step 2**: Operator Revision Store with parent-child chains
- [x] **Step 3**: Gate system (fail-closed validation)
- [x] **Step 4**: Admission control (plugin_only profile)

### Phase 2: V3 Workflow (Steps 5-8) ✅

- [x] **Step 5**: V3 service discovery & startup tuning
- [x] **Step 6**: V3 accuracy evaluation (external NV reference)
- [x] **Step 7**: V3 performance measurement (no comparison)
- [x] **Step 8**: V3 release (Gate-driven, Harbor + ModelScope/HF)

### Phase 3: V4 Optimization (Steps 9-11) ✅

- [x] **Step 9**: V4 two-phase reduction (performance search + accuracy backtrack)
- [x] **Step 10**: V4 release (Gate-driven, fallback to V3)
- [x] **Step 11**: Agent integration (suggest-verify-commit loop)

### Phase 4: Test Coverage (Step 12) ✅

- [x] **Step 12**: Unit tests for all core components (38 tests, 100% passing)
  - Artifact Registry (6 tests)
  - Operator Revision Store (7 tests)
  - Gates (5 tests)
  - Admission (5 tests)
  - V3 Startup (15 tests)

### Phase 5: Legacy Code Removal (Step 13) ✅

- [x] **Step 13a**: Create safety net (archive branch + tag)
- [x] **Step 13b**: Update `inspect_env.py` classification logic
  - Replaced `classify_entry_image_type()` with Plugin-only admission
  - Changed return format: `{accepted, rejection_reasons, profile, ...}`
  - Updated `output_report()` to show admission validation results
- [x] **Step 13c**: Remove dual pipeline routing from `run_pipeline.sh`
  - Removed `PIPELINE_BRANCH` logic (A/B/native branches)
  - Removed `BRANCH_DIRECTIVE` generation
  - Removed V1 baseline three-choice gate logic
  - Simplified segment 2/4 prompts to Plugin-only workflow
  - Removed branch-specific plugin installation directives
- [x] **Step 13d**: Plugin-only admission gate
  - Added fail-fast admission check at pipeline start
  - Exits with error if any component missing

**Status**: In Progress - Legacy code removal complete for admission and routing logic

**Remaining**:
- Archive old operator tools (`operator_search.py`, `operator_reduction.py`)
- Update CLAUDE.md to remove Branch A/B references
- Document migration guide for existing users

### Phase 6: LangGraph Migration (Step 14) 🔜

- [ ] **Step 14**: Migrate shell orchestration to LangGraph workflow engine

### Phase 7: Production Validation (Step 15) 🔜

- [ ] **Step 15**: End-to-end test with real container

---

## Test Status

**Total Tests**: 38  
**Passing**: 38  
**Failing**: 0  
**Pass Rate**: 100%

### Test Breakdown

| Component | Tests | Status |
|-----------|-------|--------|
| Artifact Registry | 6 | ✅ All passing |
| Operator Revision Store | 7 | ✅ All passing |
| Gates (fail-closed) | 5 | ✅ All passing |
| Admission Control | 5 | ✅ All passing |
| V3 Startup | 15 | ✅ All passing |

---

## Architecture Highlights

### 1. Artifact-Backed Facts
- All critical data (accuracy results, performance metrics, operator lists) stored as Artifacts
- Content hash verification ensures integrity
- Gates fail-closed on missing/corrupted Artifacts

### 2. Immutable Operator Revisions
- Parent-child chain tracks operator evolution
- Each revision records: disabled ops, category, reason, parent_id
- Cumulative disable tracking by category (accuracy/performance/compatibility)

### 3. External NV Reference
- Sole accuracy baseline (no local V1)
- Fail-closed if missing
- Per-dataset evaluation and qualification

### 4. V3 Performance Measurement-Only
- No comparison, no ratio, no Gate
- Records absolute values: throughput, TTFT, TPOT
- Serves as baseline for V4 comparison

### 5. V4 Two-Phase Optimization
- Phase 1: Performance search (greedy, no accuracy testing)
- Phase 2: Accuracy backtrack (test from best to worst until qualified)
- V4 success criteria: throughput > V3 + ≥1 operator + accuracy qualified

### 6. Plugin-Only Admission
- Requires all 4 components: vllm + flaggems + flagtree + vllm-plugin-FL
- Rejects any incomplete configuration
- Single workflow path (no A/B branches)

---

## Files Modified (Step 13)

### Core Logic
- `skills/flagos-pre-service-inspection/tools/inspect_env.py`
  - Replaced dual pipeline classification with Plugin-only admission
  - Updated `classify_entry_image_type()` function
  - Updated `collect_all()` to use new admission format
  - Updated `output_report()` to display admission results

### Orchestration
- `prompts/run_pipeline.sh`
  - Removed `ENTRY_IMAGE_TYPE` and `PIPELINE_BRANCH` logic (lines ~1274-1302)
  - Removed `BRANCH_DIRECTIVE` case statement (lines ~1319-1332)
  - Added Plugin-only admission gate with fail-fast exit
  - Removed `IS_NATIVE` flag
  - Simplified segment 2 prompt (removed branch directives)
  - Removed V1 three-choice gate logic (lines ~1620-1664)
  - Removed segment 4 branch detection (lines ~2370-2396)
  - Simplified segment 4 plugin directive

### Safety Net
- Created archive branch: `archive/legacy-dual-pipeline`
- Created tag: `legacy-code-before-cleanup`

---

## Next Steps

1. **Archive legacy operator tools** (Step 13 continued)
   - Move `operator_search.py` to `archive/`
   - Move `operator_reduction.py` to `archive/`
   - Document replacement modules

2. **Update documentation** (Step 13 continued)
   - Remove Branch A/B from CLAUDE.md
   - Document Plugin-only workflow
   - Write migration guide

3. **LangGraph migration** (Step 14)
   - Design state machine
   - Implement Agent nodes
   - Implement workflow orchestration

4. **Production validation** (Step 15)
   - End-to-end test with real container
   - Validate all 15 workflow steps
   - Performance benchmarking

---

## Known Issues

None currently tracked.

---

## References

- **Design Doc**: `docs/plugin_only_workflow_refactor_plan.md`
- **Workflow Spec**: `docs/plugin_only_workflow_optimization.md`
- **Test Coverage**: `workflow/tests/`
- **Archive Branch**: `archive/legacy-dual-pipeline`
- **Cleanup Tag**: `legacy-code-before-cleanup`
