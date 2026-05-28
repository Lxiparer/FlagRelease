# Plugin Fix Pipeline — Prompt 模板

你正在执行 **Plugin Fix Pipeline**，目标是修复一个 plugin 环境的 Docker 镜像，使其能正常启动推理服务，然后采集精度性能数据并打包发布。

---

## 基本信息

- 容器名: ${CONTAINER}
- 模型名: ${MODEL}
- 镜像: ${IMAGE}
- 最大修复轮次: ${MAX_FIX_ROUNDS}

${COMMON_TOKENS}

---

## 工作流（5步，全自动）

```
1 容器准备     → 工具部署 + 模型搜索
2 环境检测     → 确认 plugin 环境就绪
3 Plugin 修复  → 自主循环修复（核心步骤）
4 精度性能评测 → GPQA 精度 + 4k1k 性能（仅记录数据）
5 打包发布     → docker commit + Harbor 推送
```

步骤 3 失败时：跳过步骤 4 和 5，仅输出诊断报告。

---

## 执行指令

### 步骤 1：容器准备

1. 验证容器 ${CONTAINER} 运行状态
2. 搜索模型权重：
   ```
   python3 skills/flagos-container-preparation/tools/check_model_local.py --model "${MODEL}" --mode container --container ${CONTAINER} --output-json
   ```
3. 部署工具脚本：
   ```
   bash skills/flagos-container-preparation/tools/setup_workspace.sh ${CONTAINER} ${MODEL} --context-template plugin_fix
   ```
4. 写入 context.yaml 和 traces/01_container_preparation.json

### 步骤 2：环境检测

1. 执行环境检测：
   ```
   docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:$PATH python3 /flagos-workspace/scripts/inspect_env.py --json"
   ```
2. 确认 env_type 为 `vllm_plugin_flaggems`
3. 确认 plugin 已安装（`vllm_plugin` 版本非空）
4. 记录 plugin 源码路径到 context.yaml
5. 如果 env_type 不是 `vllm_plugin_flaggems`，终止流程并报错

### 步骤 3：Plugin 修复（核心）

**读取 `skills/flagos-plugin-fix/SKILL.md` 获取完整修复流程指令。**

核心循环（最多 ${MAX_FIX_ROUNDS} 轮）：

```
for round in 1..${MAX_FIX_ROUNDS}:
    1. 清理编译缓存: rm -rf /root/.triton/cache/ /tmp/triton_cache/ /root/.flaggems/code_cache/
    2. 启动服务（plugin 模式）: start_service.sh --mode flagos
    3. 等待服务: wait_for_service.sh --timeout 180 --max-timeout 600
    4. 成功 → 设 plugin_fix.final_status=fixed, workflow.service_ok=true → 退出循环
    5. 失败 → 读崩溃日志 → 分析 traceback → 定位 plugin 源码问题
    6. 直接修改容器内 plugin 代码修复问题
    7. 记录本轮修复到 context.yaml 的 plugin_fix.fix_history[]
```

**修复策略优先级**：
1. ImportError → 修复 import 路径
2. AttributeError → 适配 API 变更
3. 算子注册错误 → 修正注册逻辑或禁用问题算子
4. Triton 编译错误 → 修正 kernel 或禁用算子
5. 其他 RuntimeError → 根据 traceback 修复

**终止条件**：
- 服务启动成功 → `fixed`
- 达到最大轮次 → `max_rounds_exceeded`
- 连续 2 轮相同错误无新思路 → `unfixable`

**约束**：
- 只能修改 plugin 代码，禁止改 vllm/flaggems 核心
- 每轮必须有明确变更记录
- 禁止降级到非 plugin 模式

### 步骤 4：精度性能评测

**前置条件**：`workflow.service_ok=true`，否则跳过。

1. 确保服务运行中（步骤 3 成功后服务应仍在运行）
2. 精度评测（GPQA Diamond）：
   ```
   docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:$PATH python3 /flagos-workspace/scripts/fast_gpqa.py \
     --port $PORT --model-name '$MODEL_NAME' --output-dir /flagos-workspace/results/ --output-name plugin_fix_accuracy --json"
   ```
3. 停止服务，重启容器释放 GPU
4. 重新启动服务
5. 性能评测（4k1k quick）：
   ```
   docker exec ${CONTAINER} bash -c "PATH=/opt/conda/bin:$PATH python3 /flagos-workspace/scripts/benchmark_runner.py \
     --port $PORT --model-name '$MODEL_NAME' --mode quick --output-dir /flagos-workspace/results/ --output-name plugin_fix_performance --json"
   ```
6. 记录结果到 context.yaml（仅记录，不做达标判定）
7. 停止服务

### 步骤 5：打包发布

1. 停止容器内所有推理服务
2. Docker commit：
   ```
   docker commit ${CONTAINER} harbor.baai.ac.cn/flagrelease-public/${MODEL_REPO}:${DATE_TAG}-plugin-fix
   ```
3. Docker push 到 Harbor：
   ```
   docker login harbor.baai.ac.cn -u ${HARBOR_USER} -p ${HARBOR_PASSWORD}
   docker push harbor.baai.ac.cn/flagrelease-public/${MODEL_REPO}:${DATE_TAG}-plugin-fix
   ```
4. 写入 release 信息到 context.yaml
5. 生成最终报告

---

## 通用规则

${COMMON_PLAN_STEPS}

**进度输出格式**：
- `[步骤1] 容器准备 — 开始` / `[步骤1] 容器准备 — 完成 (耗时)`
- `[步骤3] Plugin 修复 — 第 1/5 轮`
- `✓ 修复成功：修改了 xxx.py 第 N 行`
- `✗ 第 2 轮仍失败：AttributeError: ...`

**context.yaml 更新**：使用 update_context.py，禁止手写 Python 操作 yaml。

**服务等待策略**：wait_for_service.sh 前台阻塞执行，Bash timeout=600000。
