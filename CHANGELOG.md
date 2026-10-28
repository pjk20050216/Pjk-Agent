# 更新日志

本文件记录项目的重要变更。

## [Unreleased]

### 新增 —— 风控策略智能运维多智能体系统

- **多智能体编排**：主 Agent 生成任务清单，按依赖分层异步委派给 6 个专业子 Agent
  （特征分析 / 规则配置 / 测试验证 / 异常排查 / 知识检索 / 数据采集），支持并发、重试、阻塞与恢复。
- **数据采集子 Agent**：`web_search` 发现公开来源、`web_fetch` 抓取网页正文、
  `dataset_write` 落结构化数据集；原始快照与报告进 VFS；带 SSRF/域名/端口/内容类型/
  大小/超时限制；搜索不可用时回退内部规则库并明确标注。
- **任务状态机**：`pending / in_progress / waiting_approval / completed / failed / blocked / skipped`，
  每次变更落库并发事件，支持 SSE 回放与跨会话恢复。
- **虚拟文件系统**：`/workspace /reports /logs /policies /skills /memories /sandbox`，
  支持读写、精确编辑、搜索、版本历史与 diff；host / sandbox 双层平面，产物需审批晋升。
- **上下文管理**：大结果与日志落 VFS，子 Agent 只回传摘要与文件引用，降低主上下文污染。
- **可插拔中间件**：缓存、重试、权限、PII 脱敏、敏感动作审批、链路追踪、Kafka 审计，
  通过 `before / around / after` Hook 组合。
- **Skills 渐进式披露**：元数据 → 正文 → 资源三级加载，内置 6 个风控技能。
- **全生命周期记忆**：Redis 短期（对话/计划/审批断点）+ Milvus 长期（经验/模板/偏好/案例），
  任务结束自动复盘蒸馏；Milvus/Redis 不可用时本地降级。
- **安全沙箱**：OpenSandbox 适配 + 本地受限子进程回退，命令白名单、高危拦截、超时整组 kill、
  输出截断、网络代理阻断、文件平面隔离。
- **人工审批**：规则发布、灰度、沙箱执行、文件晋升等高风险动作挂起审批；
  同进程 Event + 跨进程 SQLite 轮询，超时自动拒绝。
- **链路追踪与审计**：请求/规划/委派/记忆/工具/文件/审批全埋点，JSONL + Kafka 双通道，
  Kafka 不可用时本地缓冲。
- **Java 扩展桥**：Spring Boot SPI + 工具清单 + HMAC 校验，Python 自动发现并纳入审批/审计。
- **控制台**：`/harness` 任务板、实时时间线、审批、文件版本、技能、追踪、记忆。
- **测试与 CI**：42 个离线单元/端到端测试；静态检查新增 Harness 模块、技能、控制台、Java 桥检查。

### 修复 —— 阻塞性故障

- **配置文件未加载 `.env`**（`server/config.py`）
  模块通过 `os.environ` 读取密钥，但全项目缺少 `load_dotenv()` 调用。由于
  `pydantic-settings` 仅按「字段名大写」匹配环境变量，只有恰好同名的配置
  （如 `JWT_SECRET_KEY`）生效，名称不一致的（如 `DASHSCOPE_API_KEY`）全部落空；
  `provider_factory` 直接读 `os.environ`，三个提供商密钥均为空。
  **影响**：文档索引与 Agent 规划均报 `Missing credentials`，系统无法使用。
  **修复**：在任何配置读取之前调用 `load_dotenv(PROJECT_ROOT / ".env")`。
  该调用使用默认的 `override=False`，真实环境变量优先级高于 `.env`。

- **模型名不存在**（`config/settings.yaml`）
  `default_model: qwen3-max` 返回 404；`classifier_model: qwen-turbo` 持续返回 429
  （账号无额度）。
  **修复**：改为 `qwen-max` 与 `qwen-flash`（实测约 277ms，能正确输出分类 JSON）。
  `text-embedding-v3` 经实测可用，保持不变。

- **`X-User-ID` 可伪造导致越权**（`gateway/proxy.py`、`server/dependencies.py`）
  网关将认证后的 `user_id` **追加**到转发头，而后端 `Headers.get()` 取**第一个**
  匹配值，客户端自带的头因此生效。
  **影响**：任意登录用户仅需添加一个请求头，即可读写、删除其他用户的会话、
  消息、文档与 Agent 运行记录。
  **修复**：两层防御。
  1. 网关剔除调用方自带的 `x-user-id` / `x-internal-token`，由网关注入；
  2. 后端不再仅凭「来源为 localhost」信任该头，还要求内部令牌，
     使用 `hmac.compare_digest` 做常量时间比较。
  内部令牌由 JWT 密钥派生，网关与后端自动一致，无需额外配置。

### 修复 —— 网关

- **SSE 流式代理返回空响应**（`gateway/proxy.py`）
  `return StreamingResponse(...)` 位于 `async with httpx.AsyncClient()` 内部，
  上下文管理器在 `return` 处即关闭连接池，而 Starlette 要等到迭代生成器时才
  读取后端响应，此时连接已断开。
  **影响**：经网关的所有流式对话返回 0 字节（直连后端正常）。
  **修复**：客户端生命周期覆盖整个响应体，由各分支在结束时关闭。

- **LLM 并发限制失效**（`gateway/middleware.py`）
  `BaseHTTPMiddleware` 的 `call_next()` 在响应头就绪时返回，因此
  `async with semaphore` 在首个 token 产出前即释放，对流式响应无约束。
  **修复**：改为纯 ASGI 中间件，信号量覆盖完整响应生命周期。
  实测（`max_concurrent=1`，3 路并发 SSE）：修复前墙钟 1.57s、峰值并发 3；
  修复后 4.65s、峰值并发 1。
  同时将 `/api/agent/run` 由前缀匹配改为精确匹配，避免
  `GET /api/agent/runs`（历史列表，不调用 LLM）占用并发名额。

- **未认证请求绕过限流**（`gateway/main.py`、`gateway/middleware.py`）
  限流中间件位于 JWT 认证内层，而 401 由 JWT 中间件直接返回，请求无法到达限流器。
  **影响**：伪造 token 的洪水请求完全不受限流约束。
  **修复**：调整中间件顺序使限流位于 JWT 之外；限流器自行验签以按用户分桶，
  验签失败回退按 IP 计数（避免用伪造 `sub` 值分散到多个桶）。

- **401/429 响应缺少 CORS 头**（`gateway/main.py`）
  CORS 中间件最先添加，在 Starlette 的 LIFO 顺序下位于最内层，被外层中间件
  拒绝的响应不经过它。
  **影响**：浏览器只能看到笼统的跨域错误，无法获知真实状态码。
  **修复**：将 CORS 移至最外层。

- **公开路径前缀匹配过宽**（`gateway/middleware.py`）
  `PUBLIC_PREFIXES = ("/static", "/redoc")` 未带结尾斜杠，导致 `/staticsecret`、
  `/static-admin/keys` 等仅以该字符串开头的路径也被判定为公开。
  **修复**：改为 `("/static/",)`，`/redoc` 移入精确匹配列表。

- **后端不可用时返回 500 堆栈**（`gateway/proxy.py`）
  **修复**：捕获连接与超时异常，分别返回 502 / 504 及明确说明。

- **重复的 CORS 响应头**（`gateway/proxy.py`）
  `BEHIND_GATEWAY=false` 时前后端均添加 CORS 头，透传会导致浏览器收到重复头
  并拒绝响应。**修复**：转发时剔除后端返回的 CORS 相关头，由网关统一产生。

- **缺少客户端 IP 传递**（`gateway/proxy.py`）
  **修复**：补充 `X-Forwarded-For` 与 `X-Forwarded-Proto`。

- **移除不可达路由**（`gateway/main.py`）
  `@app.get("/")` 被 `/{path:path}` 通配路由遮蔽，永不执行。
  该路径实际由通配路由转发给后端返回 SPA 入口。

### 修复 —— RAG

- **Embedding 请求被服务端拒绝**（`server/llm/base.py`、`factory.py`、`services/rag_service.py`）
  `OpenAIEmbeddings` 默认 `check_embedding_ctx_length=True`，会先用 tiktoken
  分词并以**整数 token 数组**调用 `/embeddings`。OpenAI 官方接受该形式，
  DashScope 兼容模式返回 `400 InvalidParameter: contents is neither str nor list of str.`
  **影响**：文档上传必然失败（`status='error'`, `chunk_count=0`）。
  **修复**：改为按提供商可配置（`embedding_check_ctx_length`），
  DashScope 设为 `false`，OpenAI 官方保留 `true`。

- **Chroma 过滤器语法错误**（`server/services/rag_service.py`）
  `where` 顶层包含两个键（`chunk_type` 与 `doc_id`），而 Chroma 要求顶层
  只能有一个操作符，抛出 `ValueError: Expected where to have exactly one operator`。
  **影响**：只要用户勾选文档提问，检索即失败，异常被上层捕获后提示
  「文档检索暂时不可用」。
  **修复**：使用 `$and` 包裹多个条件。

- **重复索引导致分块累积**（`server/services/rag_service.py`）
  `index_document()` 未清除旧数据即写入，而 ES 侧已有等价的 `delete_by_doc`。
  **影响**：重复上传或重建索引后，同一内容被多次召回并挤占 `top_k`。
  **修复**：索引前先清除同一 `doc_id` 的旧分块，保证幂等。
  实测连续索引 3 次：修复前 3 → 6 → 9 个分块；修复后恒为 3。

- **相似度分数恒为 0**（`server/services/rag_service.py`）
  `as_retriever()` 在 `similarity_score_threshold` 模式下会丢弃相似度
  （`docs = [doc for doc, _ in ...]`），且不写回 `metadata["score"]`。
  **影响**：提示词中每个来源都标注「相关度 0.00」，与 `prompts/system_prompt.txt`
  中「低于 0.5 需谨慎引用」的防幻觉规则冲突。
  **修复**：直接调用 `similarity_search_with_relevance_scores` 获取真实分数；
  RRF 融合结果仅用于排序，展示分数回填 Chroma 真实相关度
  （此前归一化到最高分 1.00 会让所有来源都显示为完全相关）。
  移除因此不再使用的 `_normalize_scores()`。

### 修复 —— 前端

- **文档无法附加到对话**（`static/js/documents.js`、`static/css/style.css`）
  `toggleAttachDocument()` 已定义但全项目无任何调用点，`attachedDocIds`
  始终为空数组，RAG 功能在界面上不可达。
  **修复**：文档列表项可点击切换附加状态，并新增：
  勾选标记、选中态样式、未就绪文档（`processing`/`error`）的拒绝提示、
  删除按钮的事件隔离、标签移除时与列表的双向同步。

- **SSE 解析器丢弃事件**（`static/js/api.js`）
  `eventType` 声明在读取循环内部。当某个事件帧的 `event:` 行与 `data:` 行
  被分到不同网络分片时（真实网络下常见），下一次迭代以空 `eventType` 开始，
  该帧载荷被静默丢弃。
  **影响**：流式回复间歇性空白、`done` 事件丢失。实测帧被分片时
  2 个 token 全部丢失。`streamChat` 与 `streamAgent` 各有一份相同实现，
  两处均受影响。
  **修复**：抽出统一的 `consumeSSE()`，按空行分帧解析，事件类型跨迭代保持；
  兼容 CRLF、`event:` 后无空格、坏 JSON 跳过等情况。

### 修复 —— 健壮性

- **流式响应末尾空 `choices` 导致崩溃**（`server/llm/openai_compat.py`）
  部分模型（`qwen3.7-max`、`qwen3.8-max`）会在流末尾额外发送一个
  `choices` 为空数组的收尾分块（携带 usage 统计），无保护的
  `chunk.choices[0]` 会抛出 `IndexError` 并中断整个流。
  **修复**：`chat_stream` 与 `chat_stream_with_tools` 均跳过空 `choices` 分块。

### 变更

- **网关内部信任机制**：新增 `GATEWAY_INTERNAL_TOKEN` 配置项。
  留空时由 `JWT_SECRET_KEY` 派生，网关与后端自动一致。

### 新增

- `README.md` —— 项目说明、架构、快速开始、接口表、设计要点
- `LICENSE` —— MIT
- `.gitattributes` —— 统一换行符策略，标记第三方库与二进制文件
- `scripts/ci_checks.py` —— 无需外部依赖的静态检查（语法、导入、配置结构、
  前端资源、密钥泄漏扫描）
- `.github/workflows/ci.yml` —— CI：静态检查、前端语法、密钥泄漏扫描

### 安全

- 确认 `.env` 未被纳入版本控制，并在 CI 中持续校验
- 排除 `.claude/settings.local.json`（含本机路径，属机器特定配置）

### 已知问题

- 历史遗留：`documents` 表中存在一条 `status='error'` 记录
  （属主 `agenttest`），产生于 embedding 参数问题修复之前，可重新上传或删除
