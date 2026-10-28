# 风控策略智能运维多智能体系统

把原来的「单 Agent 对话 + RAG」升级为面向**风控策略运维复杂任务**的 Agent Harness 运行时：
主 Agent 规划任务、异步委派给专业子 Agent，子 Agent 在隔离沙箱中查特征、改规则、跑回测、查日志，
所有大结果沉淀到虚拟文件系统，高风险动作走人工审批，全过程可追踪、可恢复、可复盘。

> 原有对话 / RAG / 文档 / 多 LLM 提供商能力保留，Harness 作为独立运行时层接入同一个 FastAPI 服务。

## 为什么需要 Harness

单 Agent 直接对话在风控场景会遇到四个硬问题：

| 问题 | Harness 的解法 |
|---|---|
| 长任务上下文爆炸（日志、报告、规则 diff 动辄数 MB） | 大结果写 VFS，模型上下文只保留摘要 + 文件引用 |
| 一个 Agent 什么都做，提示词互相污染 | 6 个专业子 Agent 独立上下文，只回传结构化结论 |
| 规则发布 / 灰度 / 沙箱执行不可控 | 权限校验 + 人工审批 + 沙箱隔离 + 审计上报 |
| 任务中断后无法继续 | Redis 短期状态 + SQLite 快照 + 事件重放，支持跨会话恢复 |

## 核心能力

| 能力 | 说明 | 关键实现 |
|---|---|---|
| 任务规划 | 主 Agent 将目标拆成任务清单，维护 `pending / in_progress / waiting_approval / completed / failed / blocked` | [`harness/agents/orchestrator.py`](harness/agents/orchestrator.py)、[`harness/runtime.py`](harness/runtime.py) |
| 异步多智能体 | 按依赖分层，同层任务用 `asyncio` 并发，信号量限制并行度；`start()` 立即返回 | [`harness/runtime.py`](harness/runtime.py) |
| 子 Agent 委派 | 特征分析、规则配置、测试验证、异常排查、知识检索、数据采集 6 个专业 Agent | [`harness/agents/subagents/`](harness/agents/subagents/) |
| 外部数据采集 | `web_search` 发现来源、`web_fetch` 抓正文、`dataset_write` 落结构化数据集；SSRF/域名/端口/大小限制 | [`harness/tools/web.py`](harness/tools/web.py)、[`harness/integrations/web.py`](harness/integrations/web.py) |
| 虚拟文件系统 | `/workspace` `/reports` `/logs` `/policies` `/skills` `/memories`，读写/编辑/搜索/diff/版本留痕 | [`harness/vfs/store.py`](harness/vfs/store.py) |
| 上下文管理 | 子 Agent 摘要上限可配；工具观察截断；大结果只返回路径 | [`harness/agents/base.py`](harness/agents/base.py) |
| 可插拔中间件 | 缓存、重试、权限、PII 脱敏、敏感动作审批、链路追踪、审计上报 | [`harness/middleware/`](harness/middleware/) |
| Skills 技能系统 | `SKILL.md` 三级渐进式披露：元数据 → 正文 → 资源文件 | [`harness/skills/registry.py`](harness/skills/registry.py)、[`skills/`](skills/) |
| 全生命周期记忆 | Redis 存短期对话/计划/审批断点；Milvus 存长期经验/模板/偏好；任务结束自动复盘蒸馏 | [`harness/memory/`](harness/memory/) |
| 安全沙箱 | OpenSandbox 优先；不可用时本地受限子进程；主机 + 沙箱双层文件平面 | [`harness/sandbox/`](harness/sandbox/) |
| 人工审批 | 规则发布、灰度、沙箱执行、文件晋升等高风险动作挂起等待审批 | [`harness/approvals.py`](harness/approvals.py) |
| 链路追踪 | 请求 → 规划 → 委派 → 记忆 → 工具 → 文件 → 审批全埋点；Kafka 上报，失败本地缓冲 | [`harness/observability/tracing.py`](harness/observability/tracing.py) |
| Java 扩展 | Spring Boot 暴露工具清单与调用接口，Python 自动发现并纳入审批/审计 | [`harness/integrations/java_bridge.py`](harness/integrations/java_bridge.py)、[`java-bridge/`](java-bridge/) |

## 架构

```
浏览器 / API 客户端
        │  HTTP + SSE
        ▼
FastAPI + ASGI（uvicorn）
        │
        ├── /api/harness/*        Harness 运行时 API
        ├── /api/chat /agent ...  原有对话 / 单 Agent / RAG 能力
        └── /harness              Harness 控制台（任务板 / 审批 / 文件 / 追踪）
                    │
                    ▼
┌────────────────────── AgentHarness ──────────────────────┐
│ Orchestrator       任务规划（LLM + 确定性回退）           │
│ TaskBoard          任务状态机 + 依赖分层 + 并发控制        │
│ MiddlewareChain    cache → retry → permission → pii →    │
│                    sensitive_action → tracing → audit    │
│ SubAgent Pool      feature / rule / test / anomaly / kb / data │
│ VFS                host 平面 + sandbox 平面 + 版本留痕     │
│ Memory             Redis 短期 + Milvus 长期 + 经验蒸馏     │
│ Approvals          人工审批（同进程 Event / 跨进程轮询）   │
│ Sandbox            OpenSandbox / local fallback           │
│ Tracing            JSONL + Kafka                          │
│ JavaBridge         HTTP + HMAC                            │
└───────────────────────────────────────────────────────────┘
```

一次典型运行：

```
POST /api/harness/runs
  → 创建 RunSnapshot（queued）
  → 后台 asyncio.Task 启动
  → 召回长期经验 → 生成任务计划
  → 按依赖分层并发执行子 Agent
      → 子 Agent 读技能、查特征、改规则、跑回测
      → 高风险动作 → 审批单 → 挂起 → 人工通过/拒绝
      → 报告/日志/规则草稿写入 VFS（自动留版本）
  → 主 Agent 汇总最终报告
  → 复盘蒸馏写入长期记忆
  → 事件流结束（SSE），审计事件上报 Kafka
```

## 目录结构

```
harness/
  runtime.py              Harness 运行时（创建/启动/恢复/取消/事件）
  contracts.py            领域契约（任务、结果、审批、事件、文件、记忆）
  config.py               harness 配置（YAML + HARNESS_* 环境变量）
  store.py                SQLite 运行快照与审批单仓储
  events.py               事件总线（JSONL 回放 + SSE 广播）
  approvals.py            人工审批（同进程事件 + 跨进程轮询）
  agents/
    base.py               子 Agent 基类（LLM 工具循环 + 确定性回退）
    orchestrator.py       任务规划与最终汇总
    subagents/            6 个专业子 Agent（含数据采集）
  middleware/
    base.py               Hook 协议与洋葱式执行链
    builtin.py            缓存/重试/权限/PII/审批/追踪/审计
  vfs/store.py            虚拟文件系统（双层平面 + 版本）
  memory/                 Redis 短期 / Milvus 长期 / 蒸馏
  skills/registry.py      渐进式披露技能加载
  sandbox/                OpenSandbox + 本地受限沙箱 + 命令策略
  tools/                  工具协议、风控域工具、Java 工具包装
  observability/tracing.py 链路追踪
  integrations/           Kafka、Java 桥
  api/                    FastAPI 路由与请求模型
skills/                   7 个内置技能（含数据采集 SOP、SKILL.md + 模板）
java-bridge/              Spring Boot 工具扩展样例（SPI + HMAC）
tests/                    离线单元测试与端到端测试
static/harness.html       控制台
docs/harness-architecture.md
docs/harness-api.md
```

## 快速开始

### 1. 安装

```bash
uv sync

# 可选：生产级长期记忆与 Kafka 上报
uv pip install -r requirements-harness.txt
```

### 2. 配置

```bash
cp .env.example .env
```

至少配置一个 LLM 密钥（`DASHSCOPE_API_KEY` / `OPENAI_API_KEY`）。
**没有密钥也能跑**：规划、子 Agent 执行、报告生成都会走确定性回退路径，适合本地演示与 CI。

Harness 配置优先读 `config/settings.yaml` 的 `harness:` 段，其次可用 `HARNESS_*` 环境变量覆盖，
完整字段见 [`.env.example`](.env.example) 与 [`config/settings.yaml`](config/settings.yaml)。

### 3. 启动

```bash
# 网关 8000 + 后端 8001（推荐）
python run.py

# 或仅后端（含 /docs）
python main.py
```

- 控制台：<http://localhost:8000/harness>
- 接口文档：<http://localhost:8000/docs>
- 原有对话工作台：<http://localhost:8000/>

### 4. 完全离线演示

```bash
# Linux / macOS
HARNESS_LLM_ENABLED=false \
HARNESS_SANDBOX_PROVIDER=local \
HARNESS_MILVUS_ENABLED=false \
HARNESS_KAFKA_ENABLED=false \
python main.py
```

```powershell
# Windows PowerShell
$env:HARNESS_LLM_ENABLED="false"
$env:HARNESS_SANDBOX_PROVIDER="local"
$env:HARNESS_MILVUS_ENABLED="false"
$env:HARNESS_KAFKA_ENABLED="false"
python main.py
```

## Harness API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/harness/status` | 运行时状态（并行度、记忆、沙箱、Kafka、Java 桥） |
| POST | `/api/harness/runs` | 创建并异步启动运行，立即返回 run 快照 |
| POST | `/api/harness/runs/stream` | 创建并直接返回 SSE 事件流 |
| GET | `/api/harness/runs` | 当前用户运行列表 |
| GET | `/api/harness/runs/{id}` | 运行详情（计划、结果、审批、trace id） |
| POST | `/api/harness/runs/{id}/resume` | 恢复/重试（跨会话恢复） |
| POST | `/api/harness/runs/{id}/cancel` | 取消运行 |
| GET | `/api/harness/runs/{id}/events` | SSE：历史回放 + 实时事件（`after_seq` 续传） |
| GET | `/api/harness/runs/{id}/events/replay` | JSON 事件回放 |
| GET | `/api/harness/runs/{id}/tasks` | 任务清单与状态 |
| GET | `/api/harness/runs/{id}/files` | 虚拟文件系统目录 |
| GET | `/api/harness/runs/{id}/file` | 读取文件 |
| POST | `/api/harness/runs/{id}/files` | 写入文件（留版本） |
| GET | `/api/harness/runs/{id}/file/history` | 版本历史 |
| GET | `/api/harness/runs/{id}/file/diff` | 版本 diff |
| GET | `/api/harness/runs/{id}/traces` | 链路 span |
| GET | `/api/harness/approvals` | 待审批列表 |
| POST | `/api/harness/approvals/{id}/decision` | 通过 / 拒绝 |
| GET | `/api/harness/skills` | 技能元数据（渐进式披露第一级） |
| GET | `/api/harness/skills/{name}` | 技能正文（第二级） |
| GET | `/api/harness/skills/{name}/resource` | 技能资源（第三级） |
| GET | `/api/harness/memory/search` | 长期记忆检索 |
| GET | `/api/harness/memory/recent` | 最近记忆 |
| POST | `/api/harness/memory` | 手工沉淀一条经验 |
| GET | `/api/harness/agents` | 子 Agent 目录与工具集 |
| GET | `/api/harness/tools` | 工具目录（含风险与审批动作） |
| GET | `/api/harness/sandbox/status` | 沙箱 provider、策略与会话 |

示例：

```bash
TOKEN="<access_token>"

curl -s -X POST http://localhost:8000/api/harness/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"objective":"分析 R_RISK_001 规则上线后的误杀问题，给出调整建议、回归测试与效果对比结论"}'

# 实时事件
curl -N "http://localhost:8000/api/harness/runs/<run_id>/events?after_seq=0" \
  -H "Authorization: Bearer $TOKEN"
```

更完整的请求/响应与 SSE 事件说明见 [`docs/harness-api.md`](docs/harness-api.md)。

## 数据采集

当用户目标包含“抓取 / 采集 / 搜索公开新闻、公告、舆情、资料”等意图时，编排器会创建
`data_collection` 子 Agent，而不是误触发规则变更链路：

1. `web_search`：复用博查搜索发现候选来源（需要 `BOCHA_API_KEY`）；
2. `web_fetch`：抓取网页正文，HTML 转文本，带 SSRF、域名/端口、内容类型、超时与大小限制；
3. 原始正文写 `/logs/web/*.txt`，结构化记录写 `/workspace/data/dataset_*.json`；
4. 生成 `/reports/data_collection_*.md`，主 Agent 只拿到摘要与文件路径。

每条记录至少包含 `title / url / source / fetched_at / excerpt / raw_path`。
外部搜索不可用且没有提供 URL 时，默认回退到内部规则库生成数据集，并在报告中明确标注，
不会伪造“已抓取网页”。

安全边界：

- 只允许 http/https，默认只允许 80/443；
- 拒绝回环、私网、链路本地、保留地址，重定向逐跳复验（SSRF 防护）；
- 支持 `HARNESS_WEB_ALLOWED_DOMAINS` 域名白名单与 `blocked_domains` 黑名单；
- 单页最大字节、最大字符、超时、重定向次数均可配；
- 抓取行为走工具 Hook，产生追踪与审计事件；数据集写入受 `file.write` 权限约束；
- 不绕过登录、付费墙、验证码，不抓取隐私或明确禁止抓取的内容。

## 安全与合规

**沙箱**
- 命令白名单 + 高危正则拒绝（下载执行、反弹 shell、破坏性删除、提权）；
- 独立进程组 + 超时整组 kill + 输出截断；
- 未允许外网时注入不可达代理，阻断常见 HTTP 出口（纵深防御）；
- 沙箱文件平面与主机平面隔离，产物需 `sandbox_promote` 并经审批才能进入主工作区。
- 本地沙箱**不是内核级隔离**，生产请使用 OpenSandbox / 容器运行时。

**人工审批**
- 默认必须审批：`rule.publish`、`rule.gray_release`、`sandbox.exec`、`file.promote`、`policy.write`、`java.tool.invoke`；
- 审批单含动作、目标、风险、原因、参数快照，支持超时自动拒绝；
- 审批通过/拒绝均写入审计事件；跨进程通过 SQLite 轮询继续等待。

**权限与脱敏**
- 角色：`viewer` / `operator` / `risk_admin` / `admin`（可用 `HARNESS_ADMIN_USERS` 指定管理员）；
- `viewer` 无法执行发布、灰度、沙箱、文件晋升等动作；
- 模型调用与文件写入前检测手机号、身份证、银行卡、邮箱，默认脱敏（`HARNESS_PII_REDACT`）。

**审计与追踪**
- 工具、文件、任务状态、审批、运行结束均产生审计事件；
- Kafka 优先，失败落 `data/harness/audit/*.jsonl`；
- 链路 span 落 `data/traces/{run_id}.jsonl`，包含父子关系、耗时、状态。

## 降级矩阵

| 依赖 | 缺失/不可用时的行为 |
|---|---|
| LLM | 规划、子 Agent、蒸馏全部走确定性回退；报告仍完整生成 |
| Redis | 短期记忆退化为进程内 KV + `data/harness/state/short_term.json` |
| Milvus / pymilvus | 长期记忆退化为本地 JSONL + 关键词/标签打分 |
| Kafka / aiokafka | 审计与链路事件落本地 JSONL 缓冲 |
| OpenSandbox | 自动切换到本地受限子进程沙箱 |
| Elasticsearch | 原有 RAG 退化为纯向量检索（与改造前一致） |
| Java 桥 | 未启用时只使用内置工具，不影响运行 |
| 博查搜索 | `web_search` 返回空；数据采集回退内部规则库（可关闭 `offline_fallback`） |
| 目标网页 | 记录失败原因（超时/403/内容类型/域名限制），不阻塞整条运行 |

## 测试

```bash
# 离线单元 + 端到端测试（不依赖 Redis / ES / Milvus / Kafka）
python -m unittest discover -s tests -v

# 静态检查（语法、导入、配置、技能、前端、密钥扫描）
python scripts/ci_checks.py
```

覆盖：VFS 版本/越界/文件 Hook、沙箱策略/超时、中间件缓存/权限/PII/审批、
技能渐进披露、记忆检索/蒸馏、编排依赖、运行时端到端、HTTP API。

## 设计要点

**子 Agent 上下文隔离**：子 Agent 只收到任务、相关经验与技能摘要，不继承主对话；
工具结果在消息中截断，完整内容落 VFS；回传主 Agent 的 `summary` 默认不超过 1200 字符。

**事件即状态**：每次状态变更先落库、再发事件，事件带单调 `seq` 与 `trace_id`；
SSE 断线可用 `after_seq` 续传，前端与审计消费同一份事件流。

**幂等与可恢复**：运行快照整体存 JSON，任务完成状态持久化；恢复时跳过已完成任务，
重置中断/失败任务；审批等待支持同进程 Event 与跨进程轮询两种唤醒方式。

**确定性回退**：所有依赖外部模型/服务的环节都有离线实现，保证 CI 与演示可复现；
风控域数据源（[`harness/tools/risk_data.py`](harness/tools/risk_data.py)）使用固定种子。

**渐进式披露**：技能目录只注入名称与一句话描述；Agent 需要时再加载正文与资源，
避免一次性把全部 SOP/模板塞进上下文。

## 已知限制

- SQLite 适合单机；多实例部署请换 PostgreSQL，并将运行锁/审批等待改为分布式实现。
- 本地沙箱只做进程/文件/代理级隔离，不具备内核级网络隔离；生产必须使用 OpenSandbox。
- Milvus 使用动态字段 schema，`dim` 必须与当前 embedding 模型一致（默认 1024）。
- Java 桥的 HMAC 在密钥为空时跳过校验，仅限本地开发。
- 前端为原生 JS 单页，无构建流程；复杂交互建议迁移到独立前端工程。
- 回测数据为确定性模拟，接入真实特征/决策平台时替换 `harness/tools/risk_data.py`。
- `web_fetch` 不执行 JavaScript，单页正文可能被截断；DNS 校验与连接之间存在 rebinding 时间窗，
  生产环境应叠加出口代理/网络策略。
- 抓取公开网页需遵守目标站点 robots/服务条款与访问频率；涉及个人信息时必须先脱敏。

## 原有能力

- 文档上传 + Parent-Child 切分 + Chroma/ES 混合检索 + RRF 融合；
- 三层防注入（正则 → LLM 分类 → Prompt 护栏）；
- 多 LLM 提供商适配与运行时切换；
- 网关 JWT / 限流 / LLM 并发控制 / SSE 透传。

## License

本项目采用 [MIT License](LICENSE)。

`static/lib/` 下的第三方前端库保留各自原有许可：

| 文件 | 许可 | 版权 |
|---|---|---|
| `marked.min.js` | MIT | © 2011-2025 Christopher Jeffrey |
| `highlight.min.js` | BSD-3-Clause | © highlight.js contributors |
| `github-dark.min.css` | BSD-3-Clause | highlight.js 主题 |
