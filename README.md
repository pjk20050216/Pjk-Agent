# DevFlow AI

AI 驱动的技术方案与开发工作流平台 —— 上传需求文档，自动产出技术方案、任务拆解与数据库设计。

## 功能

### RAG 智能问答
上传 PDF / TXT 文档后，基于文档内容带来源标注地回答，显著降低幻觉。

- **Parent-Child 两级切分**：父块 600 字保证上下文完整，子块 200 字保证检索精度；只对子块建向量索引，命中后按 `parent_id` 回表取父块全文
- **混合检索**：Chroma 向量语义召回 与 Elasticsearch BM25 全文召回并行执行，再用 RRF（倒数排名融合）合并两路排名
- **优雅降级**：ES 不可用时自动退化为纯向量检索，对外契约不变
- **强制引用**：系统提示词要求标注 `[来源 X]`，检索不到时明确回答「文档中未提及相关内容」

### Agent 自动规划
一句话描述需求，LangGraph 状态机自动规划并串联工具，产出完整技术文档。

执行流程：`plan → tool → observe → aggregate`，信息不足时转入 `clarify` 向用户反问。

内置 5 个工具：

| 工具 | 职责 |
|---|---|
| `document_reader` | 读取文档，提取功能模块、业务实体、业务流程 |
| `tech_plan_generator` | 生成系统架构、技术栈选型、模块划分、API 设计 |
| `task_decomposer` | 拆解为 10-20 个可执行任务，含优先级与依赖关系 |
| `database_designer` | 生成 ER 图描述、建表 DDL、索引设计 |
| `web_search` | 联网检索最新信息，返回带 URL 的来源 |

### 安全防护

- **三层防注入**：正则快速过滤（零延迟）→ LLM 语义分类 → Prompt 护栏
- **拦截时机**：在保存用户消息**之前**判断，命中即 403，不污染对话历史
- **多 LLM 提供商**：适配器模式统一 OpenAI 兼容协议，支持运行时切换

## 架构

```
浏览器 ──> 网关 :8000 ──> 后端 :8001 ──> SQLite / Chroma / ES / Redis
              │
              ├─ CORS（最外层，保证 401/429 也带跨域头）
              ├─ 限流（按用户或 IP，含未认证请求）
              ├─ JWT 认证
              └─ LLM 并发控制（信号量覆盖整个流式响应）
```

**网关独立于业务**：后端只监听 `127.0.0.1`，不暴露到公网；认证、限流、并发控制在网关统一处理，后端各接口无需重复实现。

**网关↔后端互信**：网关转发时注入内部令牌，后端校验通过才信任 `X-User-ID`。仅凭「请求来自 localhost」不足以信任 —— 本机任意进程都能连接回环地址。

### 目录结构

```
gateway/              API 网关：JWT 校验、限流、LLM 并发、SSE 反向代理
server/
  main.py             FastAPI 装配 + lifespan（建表、注册工具、探测外部依赖）
  config.py           配置中枢：YAML 提供商列表 + 环境变量
  routers/            auth / conversations / chat / documents / models / agent
  services/           业务逻辑（rag_service 负责混合检索与索引）
  agent/              LangGraph 状态图 + 工具注册中心 + 5 个工具
  llm/                适配器模式：base 接口 + factory 工厂 + openai_compat 实现
  security/           三层防注入：正则 → LLM 语义分类 → Prompt 护栏
  utils/              redis / 熔断器 / ES / 联网 / SSE / 缓存 / 日志 / JWT
  models/ schemas/    SQLAlchemy 2.0 ORM + Pydantic
prompts/              system_prompt / guard_rail / rag_prompt
static/               原生 JS 单页应用（8 个模块 + marked/highlight 本地库）
docker/               带 IK 中文分词的 Elasticsearch
config/settings.yaml  LLM 提供商列表与各类开关
```

## 技术栈

| 层 | 选型 |
|---|---|
| Web 框架 | FastAPI + Uvicorn |
| Agent 编排 | LangGraph（状态机 + 检查点） |
| LLM 接入 | 原生 AsyncOpenAI + 适配器模式 |
| 向量检索 | Chroma（LangChain 集成） |
| 全文检索 | Elasticsearch 8.17（IK 中文分词） |
| 缓存 | Redis（LLM 响应 / RAG 结果 / 工具结果 / 限流） |
| 数据库 | SQLite + SQLAlchemy 2.0（ORM 声明式） |
| 前端 | 原生 JS + marked + highlight.js |
| 运行时 | Python 3.13 |

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置

复制 `.env.example` 为 `.env` 并填入密钥：

```bash
cp .env.example .env
```

| 变量 | 说明 |
|---|---|
| `JWT_SECRET_KEY` | JWT 签名密钥，**生产环境必须修改** |
| `DASHSCOPE_API_KEY` | 通义千问密钥（默认提供商） |
| `OPENAI_API_KEY` | OpenAI 密钥（可选） |
| `BOCHA_API_KEY` | 博查联网搜索密钥（可选） |
| `BEHIND_GATEWAY` | `true` 时后端关闭 `/docs` 与 CORS，由网关统一处理 |

在 `config/settings.yaml` 中配置 LLM 提供商，`active_provider` 指定默认项（索引从 0 开始）。

### 3. 启动外部依赖（可选）

未启动时相关功能自动降级，不影响基础对话：

```bash
# Elasticsearch（混合检索的全文召回；不启动则退化为纯向量检索）
cd docker && docker compose up -d --build

# Redis（缓存；不启动则缓存关闭）
docker run -d -p 6379:6379 redis
```

### 4. 运行

```bash
# 同时启动网关(8000) 与后端(8001) —— 推荐
python run.py

# 或仅启动后端（单进程，含 /docs）
python main.py
```

打开 http://localhost:8000 ，接口文档见 http://localhost:8000/docs 。

> **提示**：若本机运行代理软件（Clash / V2Ray 等），可能劫持 `127.0.0.1:8000` 的回环流量，导致请求异常缓慢或随机 429。建议将 `127.0.0.1` 加入代理绕过列表。

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` `/login` `/refresh` | 注册 / 登录 / 刷新令牌 |
| GET | `/api/auth/me` | 当前用户信息 |
| GET/POST | `/api/conversations` | 会话列表 / 创建 |
| POST | `/api/chat/{id}` | 发送消息，**SSE 流式**返回 |
| POST | `/api/documents/upload` | 上传文档并自动索引 |
| POST | `/api/agent/run` | 执行 Agent，**SSE** 推送进度 |
| GET | `/api/agent/tools` | 可用工具列表 |
| POST | `/api/models/switch` | 运行时切换 LLM 提供商 |

## 设计要点

**幂等索引**：`index_document()` 会先清除同一 `doc_id` 的旧分块，重复上传不会累积重复向量。

**按模型隔离 collection**：collection 名带 embedding 模型名的哈希后缀，切换模型后自动使用独立索引，避免向量串味。

**熔断保护**：每个 LLM 提供商持有独立熔断器（CLOSED → OPEN → HALF_OPEN），连续失败后快速失败，避免雪崩。

**同步/异步边界**：Chroma 是同步阻塞调用，检索时通过 `asyncio.to_thread` 移入线程，不阻塞事件循环。

**外部依赖全部降级可用**：Redis、ES、联网搜索任一不可用，系统仍能提供基础对话能力，仅记录告警。

## 已知限制

- SQLite 适合单机部署，多实例需换 PostgreSQL
- 会话历史未做长度裁剪，超长对话可能触及模型上下文上限
- 上传文件仅支持 PDF / TXT，最大 20MB
- 向量索引重建在后台线程执行，重建期间检索可能返回空结果

## License

本项目采用 [MIT License](LICENSE)。

`static/lib/` 下的第三方前端库保留各自原有许可：

| 文件 | 许可 | 版权 |
|---|---|---|
| `marked.min.js` | MIT | © 2011-2025 Christopher Jeffrey |
| `highlight.min.js` | BSD-3-Clause | © highlight.js contributors |
| `github-dark.min.css` | BSD-3-Clause | highlight.js 主题 |

