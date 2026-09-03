# 报告数据契约修复 - 当前进度

**Date**: 2026-09-02  
**Session**: 继续 Step 13  
**Status**: Phase 1 完成，待测试和集成

---

## 工作目标

确保 Plugin-only 工作流能产出 `generate_report.py` 期望的所有数据文件。

---

## 已完成工作 ✅

### 1. 问题分析和规划
- ✅ `docs/report_generation_data_contract_gap.md` - Gap 分析
- ✅ `docs/report_data_contract_fix_complete.md` - 初步修复记录
- ✅ `docs/complete_code_planning.md` - 完整代码规划

### 2. 域模型修复
- ✅ `workflow/domain/v3_accuracy.py`
  - 移除模拟实现（硬编码 65.2%）
  - 实现真实的 subprocess 调用 `fast_gpqa.py`
  - 添加 `_generate_comparison_file()` 方法
  - 添加 `_export_operator_config()` 方法
  
- ✅ `workflow/domain/v3_performance.py`
  - 修正文件命名：`flagos_optimized` → `v3_performance`
  
- ✅ `shared/generate_report.py`
  - 增强 V3 精度 fallback
  - 增强 V3 性能 fallback
  - 新增 V3/V4 对比文件 fallback

### 3. 后处理脚本
- ✅ `workflow/cli/generate_comparison_and_config.py`
  - 独立的 CLI 脚本
  - 读取评测结果 + NV baseline + context.yaml
  - 生成 `accuracy_compare_{candidate}.json`
  - 生成 `operator_config_{candidate}.json`
  - 完整的错误处理和日志

- ✅ `workflow/cli/test_generate_comparison.py`
  - 单元测试脚本
  - 创建模拟数据
  - 验证后处理脚本输出
  - 3个测试场景（达标/不达标/临界值）

### 4. 文档
- ✅ `docs/shell_integration_guide.md` - Shell 集成指南

---

## 待完成工作 ⏳

### Phase 2: 测试验证（估计 1-2 小时）

1. **运行单元测试**
   ```bash
   cd /home/lz/workspace/flagos_workflow/FlagRelease
   python3 workflow/cli/test_generate_comparison.py
   ```
   - 验证后处理脚本逻辑正确
   - 验证文件格式符合报告期望

2. **手动测试后处理脚本**
   - 准备模拟数据（gpqa_v3.json, nv_baseline.yaml, context.yaml）
   - 手动运行脚本
   - 检查产出文件

### Phase 3: Shell 集成（估计 1-2 小时）

3. **修改 run_pipeline.sh 段2**
   - 在步骤4（V3精度评测）后添加后处理调用
   - 处理返回码（qualified/not qualified）
   - 更新 context.yaml 的 workflow.accuracy_ok

4. **修改 run_pipeline.sh 段4**
   - 在 V4 精度评测后添加后处理调用
   - 在 V4 性能测量时使用正确命名

### Phase 4: V4 完整实现（估计 2-3 小时）

5. **V4 reduction 集成**
   - 在 `workflow/domain/v4_reduction.py` 添加评测调用
   - 调用 `fast_gpqa.py` 产出 `gpqa_v4.json`
   - 调用 `benchmark_runner.py` 产出 `v4_performance.json`
   - 调用后处理脚本产出对比和配置文件

### Phase 5: 端到端验证（估计 2-3 小时）

6. **V3 完整流程测试**
   - 准备测试容器和模型
   - 运行 V3 工作流（discovery → startup → accuracy → performance）
   - 验证文件产出：
     - `gpqa_v3.json`
     - `accuracy_compare_v3.json`
     - `v3_performance.json`
     - `operator_config_v3.json`

7. **V4 完整流程测试**
   - 运行 V4 工作流（reduction → accuracy → performance）
   - 验证文件产出（同上，v4 版本）

8. **报告生成验证**
   ```bash
   python3 shared/generate_report.py --output results/report.md
   ```
   - 检查 V3 数据完整性（精度、性能、算子数）
   - 检查 V4 数据完整性
   - 验证 fallback 逻辑生效

---

## 当前架构

### 数据流图

```
fast_gpqa.py (现有)
    ↓ 产出
gpqa_v3.json
    ↓ 被读取
generate_comparison_and_config.py (新增)
    ├─ 读取 gpqa_v3.json
    ├─ 读取 nv_baseline.yaml
    ├─ 读取 context.yaml (算子配置)
    ↓ 产出
    ├─ accuracy_compare_v3.json
    └─ operator_config_v3.json
    ↓ 被读取
generate_report.py (已增强 fallback)
    ↓ 产出
report.md (完整数据)
```

### 调用链

```
run_pipeline.sh 段2
    ↓
docker exec fast_gpqa.py → gpqa_v3.json
    ↓
docker exec generate_comparison_and_config.py
    ↓ 产出
    ├─ accuracy_compare_v3.json
    └─ operator_config_v3.json
    ↓
benchmark_runner.py → v3_performance.json
    ↓
(所有文件就绪)
    ↓
generate_report.py 读取并生成报告
```

---

## 关键决策记录

### 为什么选择后处理脚本而非域模型直接调用？

**原因**：
1. **最小改动** - 不改变现有 Shell 编排逻辑
2. **复用现有** - fast_gpqa.py 和 benchmark_runner.py 已验证
3. **解耦** - 后处理脚本是独立工具，可单独测试
4. **向后兼容** - 即使后处理失败，评测结果仍然可用

### 为什么修改域模型但不直接使用？

**原因**：
1. **未来准备** - 域模型为未来的编排层迁移做准备
2. **测试价值** - 域模型可以被单元测试，提高代码质量
3. **部分复用** - 后处理脚本可以调用域模型的部分逻辑（未实现，但可选）

---

## 风险和缓解

### 风险1: 后处理脚本路径问题

**风险**: 脚本路径硬编码，部署时可能不存在

**缓解**:
- 脚本放在 `workflow/cli/` 目录（与仓库一起部署）
- Shell 使用绝对路径：`/flagos-workspace/workflow/cli/generate_comparison_and_config.py`
- 添加路径检查：
  ```bash
  if [ ! -f /flagos-workspace/workflow/cli/generate_comparison_and_config.py ]; then
      echo "✗ 后处理脚本不存在"
      exit 1
  fi
  ```

### 风险2: NV baseline 文件格式变化

**风险**: nv_baseline.yaml 格式可能有多种变体

**缓解**:
- 后处理脚本支持两种格式：
  ```yaml
  # 格式1: 扁平
  gpqa_diamond: 66.8
  
  # 格式2: 嵌套
  datasets:
    gpqa_diamond:
      accuracy: 66.8
  ```
- 详细的错误提示

### 风险3: context.yaml 缺失算子配置

**风险**: 新容器可能没有 optimization 字段

**缓解**:
- 降级为警告而非错误
- 使用默认值填充
- 报告中仍显示"-"但不阻塞流程

---

## 测试策略

### 层级1: 单元测试（独立）
- `test_generate_comparison.py` - 后处理脚本逻辑
- 使用模拟数据，不依赖真实容器
- 快速验证（< 10秒）

### 层级2: 集成测试（容器内）
- 在真实容器中运行后处理脚本
- 使用真实的评测结果文件
- 验证与真实环境的兼容性

### 层级3: 端到端测试（完整流程）
- 完整 V3/V4 工作流
- 验证所有文件产出
- 生成并检查报告

---

## 文件清单

### 新增文件
1. `workflow/cli/generate_comparison_and_config.py` - 后处理脚本
2. `workflow/cli/test_generate_comparison.py` - 测试脚本

### 修改文件
1. `workflow/domain/v3_accuracy.py` - 真实评测实现
2. `workflow/domain/v3_performance.py` - 文件命名修正
3. `shared/generate_report.py` - Fallback 增强

### 待修改文件
1. `prompts/run_pipeline.sh` - Shell 集成
2. `workflow/domain/v4_reduction.py` - V4 评测调用

### 文档文件
1. `docs/report_generation_data_contract_gap.md` - Gap 分析
2. `docs/report_data_contract_fix_complete.md` - 修复记录
3. `docs/complete_code_planning.md` - 代码规划
4. `docs/shell_integration_guide.md` - 集成指南
5. `docs/report_fix_progress.md` - 本文档

---

## 下一步行动

### 立即（等 Bash 恢复）
1. 运行 `test_generate_comparison.py` 验证后处理脚本
2. 如果测试通过，提交当前进度
3. 修改 `run_pipeline.sh` 集成后处理调用

### 短期（本次会话或下次）
4. 测试 V3 完整流程
5. 实现 V4 集成
6. 端到端验证

### 中期（Step 13 完成后）
7. 完成 Step 13 其他收尾工作
8. 归档旧工具
9. 更新 CLAUDE.md

---

## 估计剩余工作量

- **测试验证**: 1-2 小时
- **Shell 集成**: 1-2 小时
- **V4 实现**: 2-3 小时
- **端到端验证**: 2-3 小时
- **总计**: 6-10 小时

**当前完成度**: 约 40%（规划和核心代码已完成，待测试和集成）

---

## 总结

**已完成**：
- ✅ 完整的问题分析和规划
- ✅ 域模型修复（真实评测、正确命名）
- ✅ 后处理脚本实现
- ✅ 测试脚本编写
- ✅ 报告 fallback 增强
- ✅ 详细文档

**待完成**：
- ⏳ 运行测试验证
- ⏳ Shell 集成
- ⏳ V4 完整实现
- ⏳ 端到端验证

**阻塞**：
- Bash 暂时不可用，无法运行测试
- 测试通过后即可继续集成

**恢复路径**：
1. 等 Bash 恢复
2. 运行 `python3 workflow/cli/test_generate_comparison.py`
3. 根据测试结果修复或继续
4. 集成到 Shell 编排层
5. 端到端测试
