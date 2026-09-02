# Claude Code 运行机制笔记

> 本文整理自一次答疑，围绕「上下文窗口、claude -p、harness、tool_result、阻塞执行」几个概念，
> 并结合本仓库 `run_pipeline.sh` / `start_service.sh` / `wait_for_service.sh` 的真实用法。

---

## 1. 上下文窗口（Context Window）

- 容量通常在 **200K token** 量级（具体上限受运行环境配置影响）。
- **200K 是输入 + 输出的总和**，不是纯输出。这个窗口里同时装着：
  - 系统提示（system prompt、工具定义、CLAUDE.md 等）
  - 历史对话（你的消息 + 我的回复）
  - **工具调用与返回结果**（读文件、跑命令的输出，往往占大头）
  - 当前正在生成的回复
- 每生成一轮回复，输出会变成后续「历史」，继续占用窗口。对话越长、读的文件越多，消耗越快。

### 超出上下文会怎样：自动压缩（compaction）

不是报错中断，而是：
- 把较早的上下文**总结成摘要**，连同近期未总结内容一起放进下一个窗口，从原位置继续工作。
- 代价：**早期细节会丢失**，只保留摘要级信息。
- 因此压缩后我会倾向**重新确认状态**（重读文件、看最近命令输出），而非依赖对早期上下文的记忆。

> 区分两个限制：
> - **上下文窗口（~200K）**：输入+输出的总盘子，是「记忆」边界。
> - **单次回复输出上限**（例如 16384 token）：一次回复最多生成多少 token，比窗口小得多。
>   要输出很长的代码时会分成完整功能模块发，而不是一次吐完。

---

## 2. `claude -p`（非交互 / headless 模式）

`-p` 是 `--print` 缩写，用于**一次性任务**：执行 → 打印到 stdout → 进程退出，不进入交互 REPL。

```bash
claude -p "你的 prompt"
cat error.log | claude -p "分析这个日志里的报错"   # 也可从管道读 stdin
```

要点：
- **仍是完整 agent loop**：会调用工具、多轮思考、自主完成任务，不是「只回一句就完」。
- 适合脚本 / CI/CD / cron：读 stdin、写 stdout、有 exit code。
- 输出格式可控：
  ```bash
  claude -p "..." --output-format text          # 纯文本(默认)
  claude -p "..." --output-format json          # 结构化,含元数据/耗时/成本
  claude -p "..." --output-format stream-json    # 流式,边生成边输出事件
  ```
- 会话续接：`--continue`（接最近会话）、`--resume <session-id>`。
- 权限：非交互没法弹窗询问，需预先指定，如 `--permission-mode auto` 或 `--allowedTools`。

---

## 3. harness 是什么

**harness = 包裹并驱动模型运行的那层外部程序**（运行框架/运行时外壳）。
在本项目里，harness 就是 **Claude Code CLI 进程本身**。它负责：

- 加载 `CLAUDE.md`、`skills/`、`settings.local.json`，组装成模型的上下文
- 拿到模型产生的 `tool_use`（「我要跑某命令」），**真正在你机器上执行**
- 捕获 stdout/stderr/exit code，封装成 `tool_result` 送回模型
- 处理权限询问、超时、后台任务、日志注入

> 关键：**模型自己不执行任何东西**。模型只「说」它想调用哪个工具，harness 才是动手的那一层，
> 是模型与操作系统之间的中间人。

---

## 4. tool_result 到底怎么实现的

核心结论：**它不是一条物理「通道」，而是对话消息数组里追加的一个 JSON 块。**

Messages API 里一次对话是一个 `messages` 数组，每条消息的 `content` 是**内容块列表**：
文本、图片、`tool_use`、`tool_result` 各是一种块。

### 完整闭环（一次工具调用）

**① 模型输出 assistant 消息，含 `tool_use` 块**
```json
{
  "role": "assistant",
  "content": [
    { "type": "text", "text": "我看一下服务状态" },
    { "type": "tool_use", "id": "toolu_01ABC", "name": "Bash",
      "input": { "command": "curl -s localhost:8000/health" } }
  ]
}
```
`id` 是配对钥匙。模型到此就「说完了」，并不能真的跑 curl。

**② harness 解析并真正执行**
`fork/exec` 子进程跑命令，用 pipe 接住 stdout/stderr，等结束拿 exit code。
这一步才和操作系统打交道，发生在 harness 里。

**③ harness 把结果封成 `tool_result` 块，作为一条 user 消息**
```json
{
  "role": "user",
  "content": [
    { "type": "tool_result", "tool_use_id": "toolu_01ABC",
      "content": "{\"status\":\"ok\"}\n", "is_error": false }
  ]
}
```
- `tool_use_id` 与①的 `id` 一致 → 完成「配对」。
- `role` 是 **user**（工具结果是「环境代替用户」回话），但类型是 `tool_result`，
  所以模型能区分它是机器数据、当作**不可信外部数据**处理。

**④ harness 带「更新后的完整 messages 数组」重新请求模型**
最反直觉但最关键：**没有东西被「推送」回模型**。底层模型无状态。
harness 把 [原历史 + tool_use 消息 + tool_result 消息] **整个重新 POST 一次**，
模型这次「读到」结果，继续往下生成。循环往复，直到模型不再产 tool_use、给出最终回复。
**这个循环就是 agent loop。**

```
模型 --(tool_use: id=X)--> harness
                             │ fork/exec 子进程, pipe 捕获输出/exitcode
                             ▼
harness 封 tool_result(tool_use_id=X) → 追加进 messages
        → 带【完整历史】重新 POST /messages
                             │
                             ▼
模型 读到 tool_result → 继续生成下一步
```

### 由此解释前面的疑问
- **为何占上下文**：tool_result 的 content 随每次请求发给模型，大日志直接吃 200K 预算。
- **前台阻塞 vs 后台**：阻塞指的是「②harness 等子进程」这段时间。
- **和 `-p` 无关**：`-p` 是外部启动 harness 的方式；tool_use/tool_result 循环是 harness **内部**驱动模型的机制，两层不同。

---

## 5. 本项目实战：pipeline 用 -p 启动，脚本执行时进入什么等待状态

这里有**两层不同的 claude/进程**，务必区分。

### 第一层：pipeline 用 `-p` 启动一整个长命会话
`prompts/run_pipeline.sh:814`：
```bash
claude -p "${PROMPT_SEG1}" \
    --permission-mode auto \
    --output-format stream-json \
    --verbose \
    --debug-file "${DEBUG_FILE}.seg1" \
    --max-turns 500 \
    2>&1 | tee "${LOG_FILE}" \
         | tee >(python3 stream_to_debug_log.py > "${FULL_LOG}") \
         | python3 stream_filter.py --pipeline-log ... --terminal-log ...
```
- `-p` 启动的是**一整个 agent 会话**，不是跑一句就退。
- `--max-turns 500`：允许在这一个 `-p` 进程里最多 500 轮 tool_use/tool_result，
  所以它是「长命的」，内部跑着 agent loop，直到段1（容器准备→环境检测→服务启动）全干完才退出。
- `--output-format stream-json`：把每步事件实时吐到 stdout，管道给 `stream_filter.py` 解析成人可读进度，
  因此能边跑边显示进度，而非憋到最后。

### 第二层：会话内执行脚本 → 进入的「等待状态」
当 agent loop 走到「需要执行 `start_service.sh`」：

1. 模型产 `tool_use`（Bash 工具）→ 第一层那个 `-p` 进程(harness)拦截 → `fork/exec` 子进程跑脚本
   → **harness 在此同步阻塞（waitpid）等待**。

2. 阻塞多久取决于脚本自己怎么写，这正是本项目的精巧设计：

   **服务启动脚本本体几乎不阻塞** —— `start_service.sh:253`：
   ```bash
   nohup bash -c "cd /flagos-workspace && ${CMD}" > "${LOG_FILE}" 2>&1 &
   SVC_PID=$!
   ...
   sleep 2   # 只做个存活快速检查就返回
   ```
   vllm 是常驻进程，若前台阻塞跑会一直等到超时、永远拿不到结果。
   所以用 `nohup ... &` 扔后台，脚本很快 `exit`，tool_result 立即封装返回（此刻服务只是「起来了」，还没就绪）。

   **真正的前台阻塞在 `wait_for_service.sh`** —— 循环 curl 健康检查、盯进程 CPU（`ps -p $PID`），
   直到服务 ready 或超时才退出。调用它时：
   - harness 在 `waitpid` 上**同步阻塞**，等子进程结束
   - stdout 被 pipe 持续捕获，但 **tool_result 要等脚本 exit 才封装**
   - **模型侧不是在「跑」，而是在「等」**：停在「已发 tool_use、未收 tool_result」，无状态、不占 CPU、不轮询
   - harness 有**工具超时**兜底：脚本没在超时内退出，harness 会杀子进程并返回超时的 `is_error` 结果

### 整条链路
```
run_pipeline.sh
  └─ claude -p PROMPT_SEG1 --max-turns 500        ← 外层:长命 agent 会话(harness)
       │  stdout 以 stream-json 实时吐给 stream_filter.py(进度显示)
       │
       └─ agent loop 某一轮:
            模型产 tool_use(Bash: bash wait_for_service.sh)
                 │
            harness fork/exec 子进程, waitpid 同步阻塞   ← 「等待状态」
                 │  子进程前台轮询 curl /health 直到 ready/超时
                 │  (受 harness 工具超时 + 脚本自身超时 双重约束)
                 ▼
            子进程 exit → harness 捕获 stdout/exitcode
                 → 封 tool_result → 追加进历史 → 带全量历史重新请求模型
                 │
            模型读到 tool_result → 下一轮(服务已就绪,继续)
```

### 三句话总结
- **`-p` 确实是 pipeline 的启动方式** —— 但靠 `--max-turns 500` + `stream-json` 撑起一整个长命 agent 会话，不是跑一句就退。
- **执行脚本后的「等待状态」** —— 是 **harness 在 waitpid 上同步阻塞**等子进程；模型侧「已发 tool_use、静止等待下一轮请求」，不耗算力。
- **阻塞多久由脚本决定** —— 服务用 `nohup &` 后台化（不阻塞），把「等就绪」隔离到 `wait_for_service.sh` 这个专门的前台阻塞轮询里，并由 harness 工具超时兜底。

---

## 附：相关文件索引
| 作用 | 文件 |
|------|------|
| pipeline 主入口（`-p` 启动各段） | `prompts/run_pipeline.sh` |
| 交互式入口（`claude "..."`） | `start_deployment.sh` |
| stream-json 事件流 → 人可读进度 | `prompts/stream_filter.py` |
| stream-json → 全量 debug 日志 | `prompts/stream_to_debug_log.py` |
| 服务启动（`nohup &` 后台化，不阻塞） | `skills/flagos-service-startup/tools/start_service.sh` |
| 等待服务就绪（前台阻塞轮询） | `skills/flagos-service-startup/tools/wait_for_service.sh` |
