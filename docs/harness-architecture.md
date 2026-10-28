# 风控策略智能运维多智能体系统 · 架构说明

本文档描述改造后的运行时分层、关键数据结构、状态机、扩展点与降级策略。
面向需要二次开发或接入生产风控平台的工程师。

## 1. 设计目标

1. **长任务稳定**：日志/报告/规则 diff 不进入模型上下文，只保留摘要与文件引用；
2. **多智能体隔离**：每个子 Agent 独立上下文，失败不拖垮整条运行；
3. **风险动作可控**：权限 → 审批 → 沙箱 → 审计，四道闸门；
4. **可恢复**：进程重启、审批挂起、SSE 断线后都能继续；
5. **可离线**：LLM / Redis / Milvus / Kafka / OpenSandbox 任一缺失都有确定性回退；
   `HARNESS_LLM_ENABLED=false` 可强制关闭 LLM，用于演示、CI 与故障演练；
6. **可扩展**：新增 Agent、工具、技能、中间件、Java 工具不需要改运行时。

非目标：替代真实风控决策引擎；本项目提供编排与安全边界，领域数据通过工具适配。

## 2. 分层

```
┌─────────────────────────────────────────────────────────────┐
│ API 层（harness/api/router.py）                             │
│  FastAPI 路由、鉴权、SSE、请求模型、按用户隔离运行           │
├─────────────────────────────────────────────────────────────┤
│ 运行时层（harness/runtime.py）                              │
│  RunSnapshot 生命周期、后台 asyncio.Task、依赖分层、并发控制 │
├─────────────────────────────────────────────────────────────┤
│ 编排层（agents/orchestrator.py）                            │
│  目标 → TaskPlan；子 Agent 结论 → 最终报告                  │
├─────────────────────────────────────────────────────────────┤
│ 执行层（agents/base.py + agents/subagents/）                │
│  LLM Function Calling 循环 / 确定性 heuristic 回退           │
├─────────────────────────────────────────────────────────────┤
│ 能力层                                                      │
│  VFS · Memory · Skills · Sandbox · Approvals · Tools · Web  │
├─────────────────────────────────────────────────────────────┤
│ 横切层（middleware/ + observability/ + integrations/）       │
│  缓存/重试/权限/PII/审批/追踪/审计/Kafka/Java 桥             │
├─────────────────────────────────────────────────────────────┤
│ 基础设施（server/ 既有）                                     │
│  FastAPI、SQLAlchemy、Redis、LLM 适配器、RAG、安全护栏       │
└─────────────────────────────────────────────────────────────┘
```

依赖方向自上而下；能力层不反向依赖 API/运行时。唯一例外是 `harness/store.py`
使用 `server.database` 的会话工厂。

## 3. 任务状态机

```
                 ┌──────────────┐
                 │   pending    │◄──────────────┐
                 └──────┬───────┘               │ 重试 / 恢复
                        │ 开始                  │
                 ┌──────▼───────┐               │
                 │ in_progress  │───────────────┘
                 └──┬───┬───┬───┘
        高风险动作  │   │   │ 执行失败且无重试
                    │   │   └──────────────► failed
                    │   │
                    │   └── 成功 ───────────► completed
                    │
              ┌─────▼────────────┐
              │ waiting_approval │──拒绝──► failed
              └─────┬────────────┘
                    │ 通过
                    └──────► in_progress
```

运行状态：

```
queued → planning → running ⇄ waiting_approval
                         ├──► completed
                         ├──► failed
                         └──► cancelled
```

- 任务终态：`completed / failed / blocked / skipped`；
- 依赖任务失败时，下游任务置 `blocked`，不会无限等待；
- `resume()` 会把 `failed / blocked / in_progress / waiting_approval` 重置为 `pending`，
  已完成任务保留并从快照重建结果。

## 4. 一次运行的内部时序

```
API.create_run
  └─ store.save_run(snapshot) + event run.created
API.start
  └─ asyncio.create_task(_run_job)
       └─ middleware RUN_START
            └─ _execute_core
                 1. memory.recall(objective)          → memory.recalled
                 2. orchestrator.plan(...)            → plan.created
                 3. while pending:
                      ready = 依赖已满足的 pending
                      asyncio.gather(run_task × N)    → task.started/completed
                        └─ build_registry(agent toolset)
                        └─ BaseAgent.execute
                             └─ LLM loop / heuristic
                             └─ tool calls → middleware → sandbox/approval
                 4. orchestrator.synthesize(...)       → 写 /reports/final_report_*.md
                 5. memory.distill(snapshot, results) → memory.distilled
                 6. status=completed                 → run.completed
       └─ middleware RUN_END（审计 run.completed）
```

并发控制：`asyncio.Semaphore(max_parallel_subagents)` 限制同层子 Agent 数量；
不同层天然串行（依赖屏障）。

## 5. 上下文管理

主 Agent 上下文增长只来自：

- 任务标题/描述（计划阶段一次性写入）；
- 每个子 Agent 的 `summary`（默认 ≤ 1200 字符）与 `findings`（最多 8 条）；
- VFS 文件路径引用。

不进入主上下文：工具原始输出、日志快照、完整报告、规则 DSL diff。

子 Agent 内部：

- 模型消息中的工具观察统一截断到 6000 字符；
- 大结果通过 `fs_write` / `report_write` 落 VFS，返回路径；
- 结束强制生成 `SubAgentResult`，由运行时截断到 `summary_max_chars`。

估算：`harness/llm.py:estimate_tokens` 按中文 1 字≈1 token、英文 4 字符≈1 token 估算，
用于预算告警与 token 计数（非精确计费）。

## 6. 中间件链

执行模型：`before → around(operation) → after`，`before` 可短路（缓存）或抛
`HookDenied`（权限/审批拒绝）。`before` 使用 `BEFORE_*`，`after` 自动配对 `AFTER_*`；
`RUN_START` 配对 `RUN_END`。

默认顺序：

| order | 中间件 | 关心的 Hook | 作用 |
|---|---|---|---|
| 10 | `CacheMiddleware` | BEFORE_MODEL / BEFORE_TOOL | Redis + 进程内缓存只读结果 |
| 20 | `RetryMiddleware` | BEFORE_MODEL / BEFORE_TOOL | 瞬时故障指数退避重试 |
| 30 | `PermissionMiddleware` | BEFORE_TOOL / BEFORE_FILE_WRITE | 角色动作白名单 |
| 40 | `PIIMiddleware` | BEFORE_MODEL / BEFORE_FILE_WRITE | 手机号/身份证/银行卡/邮箱脱敏 |
| 50 | `SensitiveActionMiddleware` | BEFORE_TOOL | 高风险动作创建审批单并等待 |
| 80 | `TracingMiddleware` | 全部关键 Hook | 记录 span、耗时、状态 |
| 90 | `AuditMiddleware` | AFTER_TOOL / FILE / TASK / APPROVAL / RUN_END | Kafka 审计 + 结构化日志 |

扩展方式：实现 `Middleware` 子类，声明 `before_hooks / after_hooks / around`，
加入 `build_default_chain()` 或运行时自定义链。自定义中间件不应抛出非 `HookDenied`
异常，否则会被视为业务失败。

## 7. 虚拟文件系统

逻辑根目录（其余路径拒绝）：

```
/workspace   策略 DSL、脚本、分析中间产物
/reports     测试报告、效果对比、审批材料、最终报告
/logs        日志快照、沙箱输出
/policies    规则版本、草稿、发布/灰度/回滚记录
/skills      本次运行使用到的技能快照
/memories    经验沉淀草稿
/sandbox     从沙箱平面晋升的产物
```

物理布局：

```
data/harness/runs/{run_id}/
  fs/                     host 平面（权威资产）
  sandbox/                sandbox 平面（隔离执行）
  .versions/{path_hash}/  v0001.json, v0002.json ...
  events.jsonl            运行事件（回放/审计）
```

版本记录字段：`path / version / sha256 / size / author / comment / plane / deleted / created_at / content`。

安全：路径先转 POSIX、拒绝 `..`、解析后必须 `is_relative_to(plane_root)`；
单文件默认上限 2 MB；写入采用临时文件 + `replace` 原子替换。

`promote(sandbox_path, host_path)` 是唯一的跨平面通道，工具层默认要求 `file.promote` 审批。

Agent 的文件操作通过 `HookedFileSystem` 代理执行，从而真正触发
`BEFORE_FILE_READ / BEFORE_FILE_WRITE / AFTER_FILE_*` 钩子（权限、PII 脱敏、追踪、审计）；
运行时的内部写操作（run_manifest、最终报告）使用原始 VFS，避免自审批。

### 数据采集（VFS 之上的取数层）

`data_collection` 子 Agent 的链路：

```
目标/URL → web_search（博查） → web_fetch（SSRF 防护 + HTML→文本）
        → /logs/web/*.txt（原始快照）
        → dataset_write → /workspace/data/dataset_*.json（结构化）
        → /reports/data_collection_*.md（报告）
```

安全设计：

- URL 仅允许 http/https，默认仅 80/443；
- 域名白名单/黑名单，拒绝 userinfo；
- DNS 解析后拒绝回环、私网、链路本地、保留、组播地址；
- 手动跟随重定向并逐跳复验，限制跳数；
- 内容类型白名单、最大字节/字符、超时；
- 抓取结果缓存（Redis 优先，进程内兜底）；
- 抓取走工具 Hook，产生追踪/审计；数据集写入受 `file.write` 权限约束。

离线回退：搜索不可用且未提供 URL 时，若 `offline_fallback=true`，用内部规则库生成数据集，
报告明确标注 `mode=internal_rules`，不伪装成外部抓取结果。

已知边界：不执行 JavaScript、不绕过登录/付费墙/验证码；DNS 校验与连接之间存在
rebinding 时间窗，生产应叠加出口代理与网络策略。

## 8. 记忆

### 短期（Redis）

| Key | 内容 | TTL |
|---|---|---|
| `harness:st:run:{run_id}` | 运行快照 | 24h（可配） |
| `harness:st:messages:{run_id}` | 对话/观察摘要 | 24h |
| `harness:st:checkpoint:{run_id}` | 恢复断点 | 24h |
| `harness:st:approval:{approval_id}` | 审批临时状态 | 24h |
| `harness:st:temp:{run_id}:{key}` | 临时数据 | 24h |

Redis 不可用时退化为进程内 KV + `data/harness/state/short_term.json`。

### 长期（Milvus）

集合 `harness_memories`（动态字段）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | VARCHAR(64) | 主键 |
| `vector` | FLOAT_VECTOR(dim) | 文本 embedding，COSINE |
| `text` | VARCHAR | 经验正文 |
| `kind` | VARCHAR | experience / template / preference / case |
| `tags` | VARCHAR(JSON) | 标签 |
| `run_id` / `user_id` | VARCHAR / INT | 归属 |
| `created_at` | VARCHAR | ISO 时间 |
| `metadata` | VARCHAR(JSON) | 扩展字段 |

降级：本地 `data/harness/memories/memories.jsonl` + 中文 bigram 关键词打分 + 时间衰减。

### 蒸馏

运行结束调用 `distill_run()`：优先 LLM 输出 3 条以内的 JSON 经验；失败时按任务结果
生成「排查模式」「可复用 SOP」「失败复盘」。记录会同时镜像到本地 JSONL。

## 9. 沙箱威胁模型

| 威胁 | 本地沙箱 | OpenSandbox |
|---|---|---|
| 任意命令执行 | 白名单 + 高危正则 | 容器内执行 + 同样策略前置 |
| 读取主机文件 | cwd/HOME/TMP 限制在会话目录，路径逃逸拒绝 | 独立容器文件系统 |
| 外联下载 | 注入不可达代理（非内核级） | `network=none` |
| 进程残留 | 独立进程组，超时整组 kill | 会话销毁 |
| 资源耗尽 | 超时 + 输出截断 | 容器资源限制 |
| 产物污染主机 | sandbox 平面隔离，需审批 promote | 上传/下载显式接口 |

**本地沙箱仅用于开发/降级**。生产建议：

```bash
HARNESS_SANDBOX_PROVIDER=opensandbox
HARNESS_SANDBOX_BASE_URL=http://opensandbox:8080
```

OpenSandbox 契约（见 `harness/sandbox/opensandbox.py`）：

```
POST   /api/v1/sandboxes                     {image, namespace, ttl, network, metadata}
POST   /api/v1/sandboxes/{id}/exec           {command, cwd, timeout, env}
PUT    /api/v1/sandboxes/{id}/files?path=    text/plain body
GET    /api/v1/sandboxes/{id}/files?path=
DELETE /api/v1/sandboxes/{id}
```

任何网络/协议失败都会抛出 `SandboxUnavailable`，管理器销毁远程会话并切换本地沙箱重试一次。

## 10. 审批

```
request() ── 不需要审批 ──► approved(policy:auto)
    │
    └─ 需要审批 ──► pending ──► approved / rejected / expired
```

- 同进程：`decide()` 设置 `asyncio.Event`，等待者立即唤醒；
- 跨进程：等待者每 2 秒轮询 SQLite，直到状态变化或超时；
- 超时：状态置 `expired`，运行侧收到非 approved 后抛 `HookDenied`；
- 拒绝：任务失败；若运行已无执行进程，API 将运行标记为 `failed`。

审批单字段：动作、目标、风险、原因、参数快照、审批人、意见、过期时间。

## 11. 可观测性

### 事件

`events.jsonl` 每行一个 `RunEvent`：

```json
{"seq": 12, "run_id": "run_...", "type": "task.completed",
 "payload": {"task_id": "task_...", "title": "规则调整建议", "artifacts": ["/reports/..."]},
 "ts": "2026-10-26T04:14:56+00:00", "trace_id": "trace_...", "span_id": ""}
```

事件类型：`run.* / plan.created / task.* / agent.* / tool.* / file.* / approval.* /
memory.* / sandbox.* / context.compacted / error`。

### Span

`data/traces/{run_id}.jsonl`，字段含 `trace_id / span_id / parent_span_id / name / kind /
status / start_ts / end_ts / attributes / events`。`kind`：run / agent / tool / model / file /
approval / memory / internal。

### Kafka

- `harness.audit`：`schema=harness.audit.v1`，含 event / run_id / trace_id / user_id /
  actor / action / target / outcome / risk / detail / ts；
- `harness.trace`：span 原始 JSON。

Kafka 不可用时写 `data/harness/audit/{topic}.jsonl`。

## 12. Java 扩展

```
GET  /internal/harness/tools                 工具清单
POST /internal/harness/tools/{name}/invoke   {arguments, run_id, user_id}
```

HMAC：`signature = HMAC-SHA256(secret, timestamp + "." + body)`，请求头
`X-Harness-Timestamp` / `X-Harness-Signature`，时间窗 ±300 秒。

Python 侧 `build_java_tools()` 将清单包装为 `HarnessTool`，自动参与权限、审批、审计与追踪。
新增 Java 工具只需实现 `HarnessTool` 接口并标注 `@Component`，无需改 Python。

## 13. 扩展指南

### 新增子 Agent

1. 在 `harness/agents/subagents/specialists.py`（或 `data_collection.py` 这类独立文件）实现 `BaseAgent` 子类；
2. 实现 `heuristic()` 确定性路径（必须）；
3. 在 `harness/agents/subagents/__init__.py` 注册；
4. 在 `harness/tools/__init__.py` 的 `SUBAGENT_TOOLSETS` 增加工具白名单。

### 新增工具

1. 在 `harness/tools/` 下实现 `HarnessTool`（async handler + JSON Schema）；
2. 声明 `risk` 与 `action`（审批动作）；
3. 加入对应 `build_*_tools()` 与工具集。

### 新增技能

在 `skills/{name}/SKILL.md` 写 frontmatter（name/description/triggers/tags/tools），
正文写 SOP；资源放同目录子文件夹。启动时自动加载，无需注册。

### 新增中间件

实现 `Middleware` 子类，声明 Hook 集合，加入 `build_default_chain()`；
若需要包裹操作（超时/限流），覆盖 `around()`。

## 14. 一致性与并发

- 运行快照写入由进程内 `asyncio.Lock` 串行化，避免 SQLite `database is locked`；
- 事件 `seq` 由 `EventBus` 单进程内递增，启动时从 JSONL 恢复最大值；
- 任务状态变更先落库再发事件，保证 SSE 重放与 DB 一致；
- 同一 run 只允许一个后台任务；`start()` 幂等；
- 多实例部署需把运行锁、审批等待、事件总线换成分布式实现（见「已知限制」）。
