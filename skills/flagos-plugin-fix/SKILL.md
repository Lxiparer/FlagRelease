# flagos-plugin-fix — Plugin 自动修复 Skill

## 概述

当 plugin 环境的镜像无法正常启动服务时，Claude 自主分析崩溃日志、定位 plugin 源码问题、直接修改代码，循环重试直到服务跑通。

## 触发条件

- Plugin Fix Pipeline 步骤 3
- 前置：步骤 2 环境检测确认 `env_type=vllm_plugin_flaggems`，plugin 已安装但服务启动失败

## 输入

从 `context.yaml` 读取：
- `container.name` — 目标容器
- `model.container_path` — 模型路径
- `plugin.source_path` — plugin 源码路径
- `plugin_fix.max_rounds` — 最大修复轮次（默认 5）
- `service.port` — 服务端口
- `gpu.count` — GPU 数量

## 输出

更新 `context.yaml`：
- `plugin_fix.status` — success | failed
- `plugin_fix.current_round` — 实际执行轮次
- `plugin_fix.fix_history[]` — 每轮修复记录
- `plugin_fix.final_status` — fixed | unfixable | max_rounds_exceeded
- `workflow.service_ok` — 修复后服务是否可用

写入 trace：`traces/03_plugin_fix.json`

---

## 编排层指令

### 修复循环流程

```
for round in 1..max_rounds:
    1. 清理编译缓存
    2. 启动服务（plugin 模式）
    3. 等待服务就绪（wait_for_service.sh）
    4. 如果成功 → 记录 fix_status=fixed，退出循环
    5. 如果失败 → 读取崩溃日志，分析根因
    6. 定位问题代码，执行修复
    7. 记录本轮修复到 fix_history
    8. 继续下一轮
```

### 步骤详解

#### 1. 清理编译缓存

每轮开始前必须清理，确保干净状态：

```bash
docker exec $CONTAINER bash -c "rm -rf /root/.triton/cache/ /tmp/triton_cache/ /root/.flaggems/code_cache/"
```

#### 2. 启动服务

使用 `start_service.sh --mode flagos` 启动（自动加载 plugin 环境变量）：

```bash
docker exec $CONTAINER bash -c "/flagos-workspace/scripts/start_service.sh \
  --mode flagos \
  --model-path $MODEL_PATH \
  --model-name $MODEL_NAME \
  --port $PORT \
  --gpu-count $GPU_COUNT \
  --log-path /flagos-workspace/logs/plugin_fix_round_${ROUND}.log"
```

#### 3. 等待服务

```bash
docker exec $CONTAINER bash -c "/flagos-workspace/scripts/wait_for_service.sh \
  --port $PORT --model-name '$MODEL_NAME' --timeout 180 --max-timeout 600 \
  --log-path /flagos-workspace/logs/plugin_fix_round_${ROUND}.log --mode default"
```

超时设为 600 秒（修复场景允许更长等待）。

#### 4. 成功判定

`wait_for_service.sh` 返回 0 且推理验证通过 → 修复成功。

#### 5. 崩溃日志分析

读取日志文件，重点关注：
- Python traceback（最后一个 Exception）
- `flag_gems` 或 `vllm_plugin_fl` 路径出现的行
- ImportError / ModuleNotFoundError
- AttributeError（API 变更）
- RuntimeError（算子兼容性）
- Triton 编译错误

```bash
docker exec $CONTAINER bash -c "tail -200 /flagos-workspace/logs/plugin_fix_round_${ROUND}.log"
```

#### 6. 修复策略（优先级从高到低）

1. **ImportError / ModuleNotFoundError**
   - 定位 import 语句，检查模块是否存在
   - 修复：调整 import 路径、添加缺失依赖、或条件 import

2. **AttributeError（API 不兼容）**
   - 对比 vllm 版本与 plugin 期望的 API
   - 修复：适配新 API 签名、添加兼容层

3. **算子注册/调度错误**
   - 检查 plugin 的算子注册逻辑
   - 修复：修正注册参数、禁用问题算子

4. **Triton 编译错误**
   - 定位具体 kernel 文件
   - 修复：修正 kernel 代码或禁用该算子

5. **其他 RuntimeError**
   - 分析完整 traceback
   - 修复：根据具体错误修改对应代码

#### 7. 修复操作

Claude 直接通过 `docker exec` 修改容器内文件：

```bash
# 读取源文件
docker exec $CONTAINER cat /path/to/plugin/file.py

# 修改文件（使用 python3 -c 或 sed）
docker exec $CONTAINER bash -c "python3 -c \"
import pathlib
p = pathlib.Path('/path/to/plugin/file.py')
content = p.read_text()
content = content.replace('old_code', 'new_code')
p.write_text(content)
\""
```

#### 8. 记录修复历史

每轮结束更新 context.yaml：

```bash
docker exec $CONTAINER bash -c "PATH=/opt/conda/bin:\$PATH python3 /flagos-workspace/scripts/update_context.py \
  --set plugin_fix.current_round=${ROUND} \
  --set plugin_fix.status=in_progress \
  --json"
```

### 终止条件

| 条件 | 动作 |
|------|------|
| 服务启动成功 + 推理验证通过 | `final_status=fixed`, `workflow.service_ok=true` |
| 达到 max_rounds 仍失败 | `final_status=max_rounds_exceeded`, `workflow.service_ok=false` |
| 连续 2 轮相同错误且无新修复思路 | `final_status=unfixable`, `workflow.service_ok=false` |

### 失败后处理

修复失败时：
- 写入 `logs/issues_startup.log`
- 生成诊断报告到 `results/plugin_fix_diagnosis.md`
- 步骤 4（评测）和步骤 5（发布）跳过

---

## 约束

1. **禁止降级到非 plugin 模式**。目标是修复 plugin，不是绕过它
2. **禁止修改 vllm 核心代码**。只能修改 plugin 自身的代码
3. **禁止修改 flaggems 核心代码**。只能调整 plugin 与 flaggems 的交互
4. **每轮修复必须有明确的变更记录**。不允许"重试看看"
5. **修复后必须验证**。不能只改代码不重启服务验证
