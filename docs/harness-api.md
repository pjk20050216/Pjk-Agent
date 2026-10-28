# 风控策略智能运维多智能体系统 API 使用说明

Base URL：`http://localhost:8000`（经网关）或 `http://127.0.0.1:8001`（直连后端）。

除登录/注册外，所有接口都需要：

```
Authorization: Bearer <access_token>
```

控制台页面 `/harness` 本身是公开静态页，页面内的 API 请求仍携带 JWT。

## 1. 登录与令牌

```bash
curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"demo","password":"demo123"}'
```

响应中的 `access_token` 用于后续请求。

## 2. 创建运行

```bash
curl -s -X POST http://localhost:8000/api/harness/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "objective": "分析 R_RISK_001 规则上线后的误杀问题，给出调整建议、回归测试与效果对比结论",
    "mode": "auto",
    "metadata": {"ticket": "RISK-2026-1026"}
  }'
```

响应（截断）：

```json
{
  "id": "run_fe279fcc84924dc6",
  "user_id": 1,
  "objective": "分析 R_RISK_001 ...",
  "status": "queued",
  "mode": "auto",
  "plan": null,
  "result": null,
  "counters": {},
  "created_at": "2026-10-26T04:14:56+00:00",
  "updated_at": "2026-10-26T04:14:56+00:00"
}
```

`start()` 在返回前已触发后台任务，状态很快变为 `planning / running`。

## 3. SSE 事件流

```bash
curl -N "http://localhost:8000/api/harness/runs/<run_id>/events?after_seq=0" \
  -H "Authorization: Bearer $TOKEN"
```

帧格式：

```
event: plan.created
data: {"seq":4,"run_id":"run_...","type":"plan.created","payload":{"strategy":"...","tasks":[...]},"ts":"..."}
```

- `after_seq`：断线续传，从该序号之后回放；
- 每 30 秒无事件会发送 `event: heartbeat`；
- 收到 `run.completed / run.failed / run.cancelled` 后服务端结束流。

主要事件：

| 事件 | 关键 payload |
|---|---|
| `run.created` | `run_id`, `objective` |
| `run.started` | `run_id` |
| `memory.recalled` | `count`, `items[]` |
| `plan.created` | `strategy`, `planner`, `tasks[]` |
| `task.started` | `task_id`, `title`, `agent` |
| `task.updated` | `task_id`, `status`, `error` |
| `task.completed` | `task_id`, `summary`, `artifacts[]` |
| `task.failed` | `task_id`, `error` |
| `agent.delegated` / `agent.completed` | `agent`, `task_id`, `success`, `summary` |
| `tool.started` / `tool.completed` | `tool`, `agent`, `success`, `duration_ms` |
| `approval.requested` | `approval_id`, `action`, `target`, `risk` |
| `approval.decided` | `approval{...}` |
| `run.waiting_approval` | `approval_id`, `action`, `risk` |
| `file.written` | `path`, `size`, `author` |
| `memory.distilled` | `count`, `items[]` |
| `run.completed` | `report_path`, `counters` |
| `run.failed` | `error` |

JSON 回放：

```bash
curl -s "http://localhost:8000/api/harness/runs/<run_id>/events/replay?after_seq=0" \
  -H "Authorization: Bearer $TOKEN"
```

## 4. 查询运行与任务

```bash
curl -s http://localhost:8000/api/harness/runs -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8000/api/harness/runs/<run_id> -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8000/api/harness/runs/<run_id>/tasks -H "Authorization: Bearer $TOKEN"
```

任务状态：`pending / in_progress / waiting_approval / completed / failed / blocked / skipped`。

运行状态：`queued / planning / running / waiting_approval / completed / failed / cancelled`。

## 5. 恢复与取消

```bash
# 继续执行（审批通过后、进程重启后、失败重试）
curl -s -X POST http://localhost:8000/api/harness/runs/<run_id>/resume \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"approval granted"}'

# 取消
curl -s -X POST http://localhost:8000/api/harness/runs/<run_id>/cancel \
  -H "Authorization: Bearer $TOKEN"
```

恢复语义：跳过 `completed`，重置 `failed / blocked / in_progress / waiting_approval` 为
`pending`，从当前计划继续。

## 6. 审批

列出待审批：

```bash
curl -s "http://localhost:8000/api/harness/approvals?status=pending" \
  -H "Authorization: Bearer $TOKEN"
```

响应：

```json
{
  "approvals": [{
    "id": "appr_3d9408131a04463b",
    "run_id": "run_...",
    "task_id": "task_...",
    "action": "sandbox.exec",
    "target": "sandbox_exec",
    "risk": "high",
    "reason": "tool 'sandbox_exec' performs high-risk action 'sandbox.exec'",
    "payload": {"tool": "sandbox_exec", "arguments": {"command": "python validate.py"}},
    "status": "pending",
    "created_at": "...",
    "expires_at": "..."
  }]
}
```

决策：

```bash
curl -s -X POST http://localhost:8000/api/harness/approvals/<approval_id>/decision \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"approved": true, "comment": "回测通过，同意灰度 1%"}'
```

拒绝必须填写原因。审批结果会立即唤醒同进程等待者；跨进程场景由等待者轮询数据库。

默认必须审批的动作：

```
rule.publish, rule.gray_release, sandbox.exec, file.promote, policy.write, java.tool.invoke
```

开发环境可通过 `HARNESS_AUTO_APPROVE_ACTIONS=sandbox.exec` 自动放行沙箱执行。

## 7. 虚拟文件系统

```bash
# 目录（recursive=true 返回整棵树）
curl -s "http://localhost:8000/api/harness/runs/<run_id>/files?path=/reports&recursive=true" \
  -H "Authorization: Bearer $TOKEN"

# 读文件
curl -s "http://localhost:8000/api/harness/runs/<run_id>/file?path=/reports/final_report_xxx.md" \
  -H "Authorization: Bearer $TOKEN"

# 写文件（自动留版本）
curl -s -X POST http://localhost:8000/api/harness/runs/<run_id>/files \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path":"/workspace/note.md","content":"# note\n","comment":"manual"}'

# 版本历史
curl -s "http://localhost:8000/api/harness/runs/<run_id>/file/history?path=/workspace/note.md" \
  -H "Authorization: Bearer $TOKEN"

# 版本 diff
curl -s "http://localhost:8000/api/harness/runs/<run_id>/file/diff?path=/workspace/note.md&from_version=1&to_version=2" \
  -H "Authorization: Bearer $TOKEN"
```

路径必须以 `/workspace /reports /logs /policies /skills /memories /sandbox` 开头；
`..` 与越界路径会被拒绝。

## 7.1 数据采集

用户目标包含“抓取 / 采集 / 搜索公开新闻、公告、舆情”等意图时，主 Agent 会创建
`data_collection` 子 Agent：

```bash
curl -s -X POST http://localhost:8000/api/harness/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"objective":"抓取最近三个月支付风控公开新闻和监管公告，整理成数据集"}'
```

运行完成后：

- 数据集：`/workspace/data/dataset_<seq>.json`
- 原始快照：`/logs/web/*.txt`
- 报告：`/reports/data_collection_<seq>.md`

也可在 `metadata` / 任务输入中显式给出 URL（由主 Agent 规划时传入）：

```json
{"objective":"采集这几篇公开公告","metadata":{"urls":["https://example.com/notice"]}}
```

数据采集工具：

| 工具 | 说明 |
|---|---|
| `web_search` | 搜索公开来源，返回标题/URL/摘要/时间 |
| `web_fetch` | 抓取指定 URL 正文，HTML→文本，带 SSRF/域名/端口/大小/超时限制 |
| `dataset_write` | 写 JSON/CSV 数据集到 `/workspace/data`，自动留版本 |

安全说明：

- 仅 http/https，默认 80/443；
- 拒绝回环/私网/链路本地地址，重定向逐跳复验；
- 可用 `HARNESS_WEB_ALLOWED_DOMAINS` 收紧域名白名单；
- 不执行 JavaScript，不绕过登录/付费墙/验证码；
- 外部搜索不可用且未给 URL 时，默认回退内部规则库，报告标注 `internal_rules`。

## 8. 技能

```bash
# 第一级：元数据目录
curl -s http://localhost:8000/api/harness/skills -H "Authorization: Bearer $TOKEN"

# 第二级：正文
curl -s http://localhost:8000/api/harness/skills/rule-review-sop -H "Authorization: Bearer $TOKEN"

# 第三级：资源文件
curl -s "http://localhost:8000/api/harness/skills/rule-review-sop/resource?path=templates/rule_review_report.md" \
  -H "Authorization: Bearer $TOKEN"
```

## 9. 长期记忆

```bash
curl -s "http://localhost:8000/api/harness/memory/search?q=误杀%20规则&top_k=5" \
  -H "Authorization: Bearer $TOKEN"

curl -s "http://localhost:8000/api/harness/memory/recent?limit=10" \
  -H "Authorization: Bearer $TOKEN"

curl -s -X POST http://localhost:8000/api/harness/memory \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"R_RISK_001 阈值调整前先核对设备分分布","kind":"experience","tags":["规则","误杀"]}'
```

## 10. 追踪与状态

```bash
curl -s http://localhost:8000/api/harness/runs/<run_id>/traces -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8000/api/harness/status -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8000/api/harness/agents -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8000/api/harness/tools -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8000/api/harness/sandbox/status -H "Authorization: Bearer $TOKEN"
```

## 11. 错误码

| HTTP | 场景 |
|---|---|
| 401 | 缺少/无效 JWT |
| 403 | 越权访问他人运行；意图护栏拦截；角色无权限 |
| 404 | 运行/文件/审批/技能不存在 |
| 409 | 审批已被处理 |
| 400 | 路径非法、文件超限、diff 版本不存在 |
| 503 | Harness 关闭；LLM 熔断（由上层降级处理） |
| 502/504 | 网关到后端不可达/超时 |

## 12. 前端控制台

打开 <http://localhost:8000/harness>：

- 左侧：运行列表；
- 中间：任务板（待执行/进行中/已完成/异常）、实时时间线、最终报告；
- 右侧：审批、文件、技能、追踪、记忆；
- 「新建运行」支持示例目标；高风险动作会弹出审批提示。

控制台通过 SSE 订阅 `/runs/{id}/events`，断线自动带 `after_seq` 重连。
