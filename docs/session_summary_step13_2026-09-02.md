# Session Summary: Plugin-only Workflow Refactor - Step 13

**Date**: 2026-09-02  
**Branch**: `workflow-refactor`  
**Commit**: `55f0a06`

---

## Session Goal

Complete Step 13 of the Plugin-only workflow refactor: Remove legacy dual pipeline code (Branch A/B logic) and consolidate to a single Plugin-only admission path.

---

## What Was Accomplished

### 1. Safety Net Creation ✅

Created archive infrastructure before any destructive changes:

```bash
git branch archive/legacy-dual-pipeline workflow-refactor
git tag legacy-code-before-cleanup
```

This provides rollback capability if needed.

### 2. Environment Classification Refactor ✅

**File**: `skills/flagos-pre-service-inspection/tools/inspect_env.py`

**Before** (Dual Pipeline):
```python
def classify_entry_image_type(capabilities, flagtree):
    # Returns: gems_tree (Branch A) | gems_tree_plugin (Branch B) | native
    if not has_flaggems:
        entry_type = "native"
    elif has_plugin:
        entry_type = "gems_tree_plugin"  # Branch B
    else:
        entry_type = "gems_tree"  # Branch A
```

**After** (Plugin-only):
```python
def classify_entry_image_type(capabilities, flagtree):
    # Plugin-only admission: requires all 4 components
    rejection_reasons = []
    if not has_vllm: rejection_reasons.append("vllm not installed")
    if not has_flaggems: rejection_reasons.append("flaggems not installed")
    if not has_flagtree: rejection_reasons.append("flagtree not installed")
    if not has_plugin: rejection_reasons.append("vllm-plugin-FL not installed")
    
    if rejection_reasons:
        return {"accepted": False, "rejection_reasons": rejection_reasons, "profile": ""}
    return {"accepted": True, "rejection_reasons": [], "profile": "plugin_only"}
```

**Changes**:
- Replaced branch classification (A/B/native) with fail-closed admission check
- New return format: `{accepted, rejection_reasons, profile, has_flaggems, has_flagtree, has_plugin}`
- Updated `collect_all()` to use `admission` key instead of `entry_classification`
- Updated `output_report()` to display admission validation results

**Testing**:
```python
# Test 1: All components present
result = classify_entry_image_type(
    {'flaggems_installed': True, 'vllm_plugin_installed': True},
    {'installed': True}
)
# => {'accepted': True, 'profile': 'plugin_only', ...}

# Test 2: Missing plugin
result = classify_entry_image_type(
    {'flaggems_installed': True, 'vllm_plugin_installed': False},
    {'installed': True}
)
# => {'accepted': False, 'rejection_reasons': ['vllm-plugin-FL not installed'], ...}
```

### 3. Pipeline Routing Removal ✅

**File**: `prompts/run_pipeline.sh`

Removed dual pipeline routing logic:

**Lines ~1274-1302** (Removed):
```bash
# ENTRY_IMAGE_TYPE classification
# PIPELINE_BRANCH assignment (A/B/native)
case "${ENTRY_IMAGE_TYPE}" in
    gems_tree)         PIPELINE_BRANCH="A" ;;
    gems_tree_plugin)  PIPELINE_BRANCH="B" ;;
    native)            PIPELINE_BRANCH="native" ;;
esac
```

**Replaced with**:
```bash
# Plugin-only 准入验证
ADMISSION_ACCEPTED=$(python3 -c "...读取 admission.accepted...")

if [ "${ADMISSION_ACCEPTED}" != "True" ]; then
    echo "✗ Plugin-only 准入验证失败"
    # 显示 rejection_reasons
    exit 1
fi
```

**Lines ~1319-1332** (Removed):
```bash
# BRANCH_DIRECTIVE case statement
case "${PIPELINE_BRANCH}" in
    A) BRANCH_DIRECTIVE="分支 A（gems_tree 简单路径）..." ;;
    B) BRANCH_DIRECTIVE="分支 B（gems_tree_plugin 复杂路径）..." ;;
    native) BRANCH_DIRECTIVE="native 简化路径..." ;;
esac
```

**Replaced with**:
```bash
# Plugin-only 工作流说明
PROMPT_SEG2="
**Plugin-only 工作流**：准入镜像包含全部四个组件（vllm + flaggems + flagtree + vllm-plugin-FL）。
按新工作流执行：V3(Primary，全量算子) → V4(Optimized，减算子提性能)。
精度基线：外部 NV 参考（nv_baseline.yaml），无本地 V1。
"
```

**Lines ~1620-1664** (Removed):
```bash
# V1 三选强制闸门（仅分支 B 需要）
if [ "${PIPELINE_BRANCH:-}" = "B" ]; then
    # baseline_selector.py 三选状态机
    # v1_gate.py 验证真实执行痕迹
fi
```

**Replaced with**:
```bash
# Plugin-only 工作流：无需 V1 三选闸门
# Plugin-only 工作流不依赖本地 V1 基线，精度基线统一使用外部 NV 参考
```

**Lines ~2370-2396** (Removed):
```bash
# 段4 从 context 重读分支
SEG4_BRANCH=$(python3 -c "...判断 A/B...")

# 分支专属的步骤9 plugin 安装指令
if [ "${SEG4_BRANCH}" = "B" ]; then
    SEG4_PLUGIN_DIRECTIVE="分支 B 自带 plugin，禁止重装..."
elif [ "${SEG4_BRANCH}" = "A" ]; then
    SEG4_PLUGIN_DIRECTIVE="分支 A 无 plugin，照常 install..."
fi
```

**Replaced with**:
```bash
# Plugin-only 工作流：无需分支判断
SEG4_PLUGIN_DIRECTIVE="准入镜像已包含 vllm-plugin-FL。步骤9 仅需 verify。"
```

### 4. Documentation Updates ✅

**Created**:
- `docs/step_13_removal_plan.md` - Detailed removal plan and execution strategy
- Updated `docs/plugin_only_implementation_status.md` - Step 13 completion status

**Git Commit**:
```
refactor(step13): remove dual pipeline logic, implement plugin-only admission

- Updated inspect_env.py: plugin-only admission with fail-closed validation
- Updated run_pipeline.sh: removed PIPELINE_BRANCH routing, added admission gate
- Safety net: archive branch + tag
- Documentation: removal plan + status update

Step 13 substantially complete. Remaining: archive old operator tools, update CLAUDE.md.
```

---

## Files Modified

| File | Changes | Lines Changed |
|------|---------|---------------|
| `skills/flagos-pre-service-inspection/tools/inspect_env.py` | Classification logic refactor | ~60 lines |
| `prompts/run_pipeline.sh` | Pipeline routing removal | ~150 lines removed, ~30 added |
| `docs/step_13_removal_plan.md` | New file | +320 lines |
| `docs/plugin_only_implementation_status.md` | Status update | ~50 lines |

**Total**: ~8 files changed, 1236 insertions(+), 369 deletions(-)

---

## Testing

### Manual Testing

Tested new `classify_entry_image_type()` function with 3 scenarios:
1. ✅ All components present → `accepted: True`
2. ✅ Missing plugin → `accepted: False, rejection_reasons: ['vllm-plugin-FL not installed']`
3. ✅ Missing flaggems → `accepted: False, rejection_reasons: ['flaggems not installed']`

### Unit Tests

All 38 existing unit tests still passing (no regression):
- Artifact Registry: 6 tests ✅
- Operator Revision Store: 7 tests ✅
- Gates: 5 tests ✅
- Admission: 5 tests ✅
- V3 Startup: 15 tests ✅

---

## Remaining Work (Step 13 Continued)

### 1. Archive Old Operator Tools

Move legacy operator optimization scripts:
```bash
mkdir -p archive/legacy_operator_tools/
git mv skills/flagos-operator-replacement/tools/operator_search.py archive/
git mv skills/flagos-operator-replacement/tools/operator_reduction.py archive/
```

Add README explaining replacement:
- `operator_search.py` → `workflow/domain/v3_startup_tuning.py` + `workflow/domain/v3_accuracy_tuning.py`
- `operator_reduction.py` → `workflow/domain/v4_reduction.py`

### 2. Update CLAUDE.md

Remove dual pipeline references:
- Remove "分支 A (gems_tree)" descriptions
- Remove "分支 B (gems_tree_plugin)" descriptions
- Remove "native 简化流程" descriptions
- Update workflow section to Plugin-only
- Update version definitions (V1/V2 → V3/V4)
- Document new admission criteria

### 3. Migration Guide

Create `docs/migration_gems_tree_to_plugin_only.md`:
- Explain why dual pipeline was removed
- Show old vs new admission logic
- Provide migration checklist for existing users
- Document breaking changes

---

## Impact Assessment

### Breaking Changes

1. **Admission Requirements**: Now requires all 4 components (vllm + flaggems + flagtree + vllm-plugin-FL)
   - **Before**: Could run with just flaggems+tree (Branch A) or native (no flaggems)
   - **After**: Plugin is mandatory
   - **Impact**: Users with gems_tree or native images must add vllm-plugin-FL

2. **No Local V1 Baseline**: Accuracy baseline is always external NV reference
   - **Before**: Branch A had local V1 (native baseline)
   - **After**: No local V1, external NV reference only
   - **Impact**: Cannot compare against local native performance

3. **Single Workflow Path**: No branch-specific behavior
   - **Before**: Different workflows for Branch A/B/native
   - **After**: One workflow for all models
   - **Impact**: Simplified but less flexible

### Non-Breaking Changes

1. **Segment Structure**: Still 4 segments (1→2→3→4)
2. **Step Numbers**: Still steps 1-13 (but different semantics)
3. **Context Schema**: Mostly compatible (added `admission` key)

---

## Risk Mitigation

### Safety Net

- ✅ Archive branch created: `archive/legacy-dual-pipeline`
- ✅ Tag created: `legacy-code-before-cleanup`
- ✅ Rollback possible: `git checkout legacy-code-before-cleanup`

### Gradual Rollout (Recommended)

Feature flag approach (not yet implemented):
```bash
if [ "${FLAGOS_WORKFLOW_PROFILE}" = "plugin_only" ]; then
    # Use new workflow
else
    # Use legacy workflow (deprecated)
fi
```

This allows coexistence during migration period.

---

## Next Session Tasks

1. **Archive old operator tools** (~15 min)
2. **Update CLAUDE.md** (~30 min)
3. **Write migration guide** (~30 min)
4. **Begin Step 14** (LangGraph migration)

---

## Architecture Summary

### Before (Dual Pipeline)

```
inspect_env.py → classify_entry_image_type()
                 ├─ gems_tree → Branch A (simple)
                 ├─ gems_tree_plugin → Branch B (complex)
                 └─ native → Native (simplified)
                 
run_pipeline.sh → PIPELINE_BRANCH routing
                 ├─ A: V1裸启动 → V2注入 → V3切plugin → V4减算子
                 ├─ B: V1三选 → V2(2.1/2.2) → V3(3.1/3.2) → V4
                 └─ native: 精度/性能评测 only
```

### After (Plugin-only)

```
inspect_env.py → classify_entry_image_type()
                 └─ plugin_only admission (all 4 components required)
                    ├─ accepted: True → workflow proceeds
                    └─ accepted: False → fail-fast exit
                    
run_pipeline.sh → Single workflow path
                 └─ V3(Primary) → V4(Optimized)
                    ├─ V3: Full operator set, external NV baseline
                    └─ V4: Reduced operator set, V3 as baseline
```

---

## Lessons Learned

1. **Safety-first approach works**: Archive branch + tag before any destructive changes
2. **Incremental testing helps**: Testing `classify_entry_image_type()` before committing
3. **Documentation is crucial**: Step 13 removal plan made execution straightforward
4. **Legacy code removal is iterative**: Can't remove everything at once, need phased approach

---

## References

- **Implementation Plan**: `docs/plugin_only_workflow_refactor_plan.md`
- **Removal Plan**: `docs/step_13_removal_plan.md`
- **Status Doc**: `docs/plugin_only_implementation_status.md`
- **Archive Branch**: `archive/legacy-dual-pipeline`
- **Cleanup Tag**: `legacy-code-before-cleanup`
- **Commit**: `55f0a06` - refactor(step13): remove dual pipeline logic

---

**End of Session Summary**
