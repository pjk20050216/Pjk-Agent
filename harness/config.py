"""Harness 运行时配置。

设计原则：
1. 以 config/settings.yaml 的 ``harness:`` 段为基线，全部字段可用 ``HARNESS_`` 前缀
   环境变量覆盖（如 ``HARNESS_MAX_PARALLEL_SUBAGENTS=8``），便于容器化部署。
2. 配置加载是**纯本地**行为：不连接 Redis / Milvus / Kafka / OpenSandbox，
   这些外部依赖由各自适配器在首次使用时懒连接并优雅降级。
3. 通过 :func:`get_harness_config` 暴露带缓存的单例，测试可调用
   ``get_harness_config.cache_clear()`` 重置。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from server.config import PROJECT_ROOT


def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key)
    return value if value not in (None, "") else default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_int_list(key: str, default: list[int]) -> list[int]:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return list(default)
    values: list[int] = []
    for item in raw.split(","):
        try:
            values.append(int(item.strip()))
        except ValueError:
            continue
    return values or list(default)


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


class ContextConfig(BaseModel):
    """上下文预算：主 Agent 只看摘要与文件引用，大结果落 VFS。"""

    budget_tokens: int = 24000
    summary_max_chars: int = 1200
    max_history_messages: int = 20


class MemoryConfig(BaseModel):
    short_term_ttl: int = 86400
    long_term_top_k: int = 5
    distill_enabled: bool = True
    fallback_dir: Path = Field(default_factory=lambda: _resolve_path("data/harness/memories"))


class SandboxConfig(BaseModel):
    enabled: bool = True
    provider: str = "opensandbox"          # opensandbox | local
    base_url: str = "http://127.0.0.1:8080"
    api_key: str = ""
    namespace: str = "default"
    image: str = "python:3.12-slim"
    command_timeout: int = 60
    max_output_bytes: int = 65536
    allow_network: bool = False
    workspace_ttl: int = 3600
    allowed_commands: list[str] = Field(
        default_factory=lambda: [
            "python", "python3", "pytest", "cat", "ls", "dir",
            "echo", "type", "findstr", "grep",
        ]
    )
    denied_patterns: list[str] = Field(
        default_factory=lambda: [
            r"rm\s+-rf\s+/",
            r"curl\s+.*\|\s*(ba)?sh",
            r"wget\s+.*\|\s*(ba)?sh",
            r"nc\s+-e",
            r"bash\s+-i\s+>&",
            r"mkfs",
            r"dd\s+if=",
            r"chmod\s+777\s+/",
            r"shutdown|reboot",
            r"powershell\s+-enc",
        ]
    )


class MilvusConfig(BaseModel):
    enabled: bool = True
    uri: str = "http://localhost:19530"
    token: str = ""
    collection: str = "harness_memories"
    dim: int = 1024
    top_k: int = 5


class KafkaConfig(BaseModel):
    enabled: bool = True
    bootstrap_servers: str = "localhost:9092"
    topic_audit: str = "harness.audit"
    topic_trace: str = "harness.trace"
    client_id: str = "harness-runtime"
    buffer_dir: Path = Field(default_factory=lambda: _resolve_path("data/harness/audit"))


class ApprovalConfig(BaseModel):
    enabled: bool = True
    ttl_seconds: int = 1800
    auto_approve_actions: list[str] = Field(default_factory=list)
    required_actions: list[str] = Field(
        default_factory=lambda: [
            "rule.publish",
            "rule.gray_release",
            "sandbox.exec",
            "file.promote",
            "policy.write",
            "java.tool.invoke",
        ]
    )

    def requires_approval(self, action: str) -> bool:
        if not self.enabled:
            return False
        if action in self.auto_approve_actions:
            return False
        return action in self.required_actions


class TracingConfig(BaseModel):
    enabled: bool = True
    jsonl_dir: Path = Field(default_factory=lambda: _resolve_path("data/traces"))
    sample_rate: float = 1.0
    max_events: int = 5000


class JavaBridgeConfig(BaseModel):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8081"
    shared_secret: str = ""
    timeout: int = 10
    manifest_path: str = "/internal/harness/tools"


class WebConfig(BaseModel):
    """外部数据采集配置（搜索 / 网页抓取）。"""

    enabled: bool = True
    fetch_enabled: bool = True
    timeout: int = 20
    max_bytes: int = 2_000_000
    max_chars: int = 12000
    max_redirects: int = 3
    # 为空表示允许公网；SSRF 防护始终生效（拒绝回环/私网/链路本地）
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "0.0.0.0", "::1"]
    )
    allowed_ports: list[int] = Field(default_factory=lambda: [80, 443])
    user_agent: str = "AgentHarness/1.0 (+risk-strategy-ops)"
    cache_ttl: int = 900
    max_sources: int = 8
    # 外部搜索不可用且未提供 URL 时，回退到内部规则库，避免任务空转
    offline_fallback: bool = True


class HarnessConfig(BaseModel):
    """Harness 运行时总配置。"""

    enabled: bool = True
    # 显式关闭 LLM：强制所有 Agent 走确定性回退（离线演示 / CI / 故障演练）
    llm_enabled: bool = True
    workspace_root: Path = Field(default_factory=lambda: _resolve_path("data/harness"))
    skills_dir: Path = Field(default_factory=lambda: _resolve_path("skills"))
    max_parallel_subagents: int = 4
    max_tasks: int = 12
    max_tool_iterations: int = 6
    run_timeout_seconds: int = 1800
    vfs_max_file_bytes: int = 2_000_000

    context: ContextConfig = Field(default_factory=ContextConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    milvus: MilvusConfig = Field(default_factory=MilvusConfig)
    kafka: KafkaConfig = Field(default_factory=KafkaConfig)
    approvals: ApprovalConfig = Field(default_factory=ApprovalConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    java_bridge: JavaBridgeConfig = Field(default_factory=JavaBridgeConfig)
    web: WebConfig = Field(default_factory=WebConfig)

    def ensure_dirs(self) -> None:
        """创建运行时所需目录（幂等）。"""
        for path in (
            self.workspace_root,
            self.memory.fallback_dir,
            self.kafka.buffer_dir,
            self.tracing.jsonl_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _load_yaml_section() -> dict:
    yaml_path = PROJECT_ROOT / "config" / "settings.yaml"
    if not yaml_path.exists():
        return {}
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    section = data.get("harness", {})
    return section if isinstance(section, dict) else {}


def _build_config() -> HarnessConfig:
    raw = _load_yaml_section()

    # 先构造 YAML 基线，再用环境变量覆盖
    cfg = HarnessConfig.model_validate(raw)

    sandbox_raw = raw.get("sandbox", {}) if isinstance(raw.get("sandbox"), dict) else {}
    milvus_raw = raw.get("milvus", {}) if isinstance(raw.get("milvus"), dict) else {}
    kafka_raw = raw.get("kafka", {}) if isinstance(raw.get("kafka"), dict) else {}
    approvals_raw = raw.get("approvals", {}) if isinstance(raw.get("approvals"), dict) else {}
    tracing_raw = raw.get("tracing", {}) if isinstance(raw.get("tracing"), dict) else {}
    java_raw = raw.get("java_bridge", {}) if isinstance(raw.get("java_bridge"), dict) else {}
    context_raw = raw.get("context", {}) if isinstance(raw.get("context"), dict) else {}
    memory_raw = raw.get("memory", {}) if isinstance(raw.get("memory"), dict) else {}

    cfg.enabled = _env_bool("HARNESS_ENABLED", cfg.enabled)
    cfg.llm_enabled = _env_bool("HARNESS_LLM_ENABLED", cfg.llm_enabled)
    cfg.workspace_root = _resolve_path(_env_str("HARNESS_WORKSPACE_ROOT", str(cfg.workspace_root)))
    cfg.skills_dir = _resolve_path(_env_str("HARNESS_SKILLS_DIR", str(cfg.skills_dir)))
    cfg.max_parallel_subagents = _env_int("HARNESS_MAX_PARALLEL_SUBAGENTS", cfg.max_parallel_subagents)
    cfg.max_tasks = _env_int("HARNESS_MAX_TASKS", cfg.max_tasks)
    cfg.max_tool_iterations = _env_int("HARNESS_MAX_TOOL_ITERATIONS", cfg.max_tool_iterations)
    cfg.run_timeout_seconds = _env_int("HARNESS_RUN_TIMEOUT_SECONDS", cfg.run_timeout_seconds)
    cfg.vfs_max_file_bytes = _env_int("HARNESS_VFS_MAX_FILE_BYTES", cfg.vfs_max_file_bytes)

    cfg.context.budget_tokens = _env_int("HARNESS_CONTEXT_BUDGET_TOKENS", cfg.context.budget_tokens)
    cfg.context.summary_max_chars = _env_int("HARNESS_SUMMARY_MAX_CHARS", cfg.context.summary_max_chars)
    cfg.context.max_history_messages = _env_int(
        "HARNESS_MAX_HISTORY_MESSAGES", cfg.context.max_history_messages
    )

    cfg.memory.short_term_ttl = _env_int("HARNESS_MEMORY_TTL", cfg.memory.short_term_ttl)
    cfg.memory.long_term_top_k = _env_int("HARNESS_MEMORY_TOP_K", cfg.memory.long_term_top_k)
    cfg.memory.distill_enabled = _env_bool("HARNESS_MEMORY_DISTILL", cfg.memory.distill_enabled)
    cfg.memory.fallback_dir = _resolve_path(
        _env_str("HARNESS_MEMORY_FALLBACK_DIR", str(cfg.memory.fallback_dir))
    )

    cfg.sandbox.enabled = _env_bool("HARNESS_SANDBOX_ENABLED", cfg.sandbox.enabled)
    cfg.sandbox.provider = _env_str("HARNESS_SANDBOX_PROVIDER", cfg.sandbox.provider)
    cfg.sandbox.base_url = _env_str("HARNESS_SANDBOX_BASE_URL", cfg.sandbox.base_url)
    cfg.sandbox.api_key = _env_str("HARNESS_SANDBOX_API_KEY", cfg.sandbox.api_key)
    cfg.sandbox.namespace = _env_str("HARNESS_SANDBOX_NAMESPACE", cfg.sandbox.namespace)
    cfg.sandbox.image = _env_str("HARNESS_SANDBOX_IMAGE", cfg.sandbox.image)
    cfg.sandbox.command_timeout = _env_int(
        "HARNESS_SANDBOX_TIMEOUT", cfg.sandbox.command_timeout
    )
    cfg.sandbox.max_output_bytes = _env_int(
        "HARNESS_SANDBOX_MAX_OUTPUT", cfg.sandbox.max_output_bytes
    )
    cfg.sandbox.allow_network = _env_bool(
        "HARNESS_SANDBOX_ALLOW_NETWORK", cfg.sandbox.allow_network
    )
    cfg.sandbox.workspace_ttl = _env_int(
        "HARNESS_SANDBOX_TTL", cfg.sandbox.workspace_ttl
    )
    cfg.sandbox.allowed_commands = _env_list(
        "HARNESS_SANDBOX_ALLOWED_COMMANDS",
        list(sandbox_raw.get("allowed_commands", cfg.sandbox.allowed_commands)),
    )
    cfg.sandbox.denied_patterns = _env_list(
        "HARNESS_SANDBOX_DENIED_PATTERNS",
        list(sandbox_raw.get("denied_patterns", cfg.sandbox.denied_patterns)),
    )

    cfg.milvus.enabled = _env_bool("HARNESS_MILVUS_ENABLED", cfg.milvus.enabled)
    cfg.milvus.uri = _env_str("HARNESS_MILVUS_URI", cfg.milvus.uri)
    cfg.milvus.token = _env_str("HARNESS_MILVUS_TOKEN", cfg.milvus.token)
    cfg.milvus.collection = _env_str("HARNESS_MILVUS_COLLECTION", cfg.milvus.collection)
    cfg.milvus.dim = _env_int("HARNESS_MILVUS_DIM", cfg.milvus.dim)
    cfg.milvus.top_k = _env_int("HARNESS_MILVUS_TOP_K", cfg.milvus.top_k)

    cfg.kafka.enabled = _env_bool("HARNESS_KAFKA_ENABLED", cfg.kafka.enabled)
    cfg.kafka.bootstrap_servers = _env_str(
        "HARNESS_KAFKA_BOOTSTRAP", cfg.kafka.bootstrap_servers
    )
    cfg.kafka.topic_audit = _env_str("HARNESS_KAFKA_AUDIT_TOPIC", cfg.kafka.topic_audit)
    cfg.kafka.topic_trace = _env_str("HARNESS_KAFKA_TRACE_TOPIC", cfg.kafka.topic_trace)
    cfg.kafka.client_id = _env_str("HARNESS_KAFKA_CLIENT_ID", cfg.kafka.client_id)
    cfg.kafka.buffer_dir = _resolve_path(
        _env_str("HARNESS_KAFKA_BUFFER_DIR", str(cfg.kafka.buffer_dir))
    )

    cfg.approvals.enabled = _env_bool("HARNESS_APPROVALS_ENABLED", cfg.approvals.enabled)
    cfg.approvals.ttl_seconds = _env_int(
        "HARNESS_APPROVAL_TTL", cfg.approvals.ttl_seconds
    )
    cfg.approvals.auto_approve_actions = _env_list(
        "HARNESS_AUTO_APPROVE_ACTIONS",
        list(approvals_raw.get("auto_approve_actions", cfg.approvals.auto_approve_actions)),
    )
    cfg.approvals.required_actions = _env_list(
        "HARNESS_REQUIRED_APPROVAL_ACTIONS",
        list(approvals_raw.get("required_actions", cfg.approvals.required_actions)),
    )

    cfg.tracing.enabled = _env_bool("HARNESS_TRACING_ENABLED", cfg.tracing.enabled)
    cfg.tracing.jsonl_dir = _resolve_path(
        _env_str("HARNESS_TRACING_DIR", str(cfg.tracing.jsonl_dir))
    )
    cfg.tracing.sample_rate = _env_float("HARNESS_TRACING_SAMPLE_RATE", cfg.tracing.sample_rate)
    cfg.tracing.max_events = _env_int("HARNESS_TRACING_MAX_EVENTS", cfg.tracing.max_events)

    cfg.java_bridge.enabled = _env_bool("HARNESS_JAVA_BRIDGE_ENABLED", cfg.java_bridge.enabled)
    cfg.java_bridge.base_url = _env_str("HARNESS_JAVA_BASE_URL", cfg.java_bridge.base_url)
    cfg.java_bridge.shared_secret = _env_str(
        "HARNESS_JAVA_SHARED_SECRET", cfg.java_bridge.shared_secret
    )
    cfg.java_bridge.timeout = _env_int("HARNESS_JAVA_TIMEOUT", cfg.java_bridge.timeout)
    cfg.java_bridge.manifest_path = _env_str(
        "HARNESS_JAVA_MANIFEST_PATH", cfg.java_bridge.manifest_path
    )

    cfg.web.enabled = _env_bool("HARNESS_WEB_ENABLED", cfg.web.enabled)
    cfg.web.fetch_enabled = _env_bool(
        "HARNESS_WEB_FETCH_ENABLED", cfg.web.fetch_enabled
    )
    cfg.web.timeout = _env_int("HARNESS_WEB_TIMEOUT", cfg.web.timeout)
    cfg.web.max_bytes = _env_int("HARNESS_WEB_MAX_BYTES", cfg.web.max_bytes)
    cfg.web.max_chars = _env_int("HARNESS_WEB_MAX_CHARS", cfg.web.max_chars)
    cfg.web.max_redirects = _env_int(
        "HARNESS_WEB_MAX_REDIRECTS", cfg.web.max_redirects
    )
    cfg.web.allowed_domains = _env_list(
        "HARNESS_WEB_ALLOWED_DOMAINS", list(cfg.web.allowed_domains)
    )
    cfg.web.blocked_domains = _env_list(
        "HARNESS_WEB_BLOCKED_DOMAINS", list(cfg.web.blocked_domains)
    )
    cfg.web.allowed_ports = _env_int_list(
        "HARNESS_WEB_ALLOWED_PORTS", list(cfg.web.allowed_ports)
    )
    cfg.web.user_agent = _env_str("HARNESS_WEB_USER_AGENT", cfg.web.user_agent)
    cfg.web.cache_ttl = _env_int("HARNESS_WEB_CACHE_TTL", cfg.web.cache_ttl)
    cfg.web.max_sources = _env_int("HARNESS_WEB_MAX_SOURCES", cfg.web.max_sources)
    cfg.web.offline_fallback = _env_bool(
        "HARNESS_WEB_OFFLINE_FALLBACK", cfg.web.offline_fallback
    )

    # YAML 中可能只写了部分嵌套字段，model_validate 已处理；此处保留原始值用于调试
    _ = (context_raw, memory_raw, java_raw, tracing_raw)
    return cfg


@lru_cache(maxsize=1)
def get_harness_config() -> HarnessConfig:
    """返回全局 Harness 配置单例（首次调用时加载并创建目录）。"""
    cfg = _build_config()
    cfg.ensure_dirs()
    return cfg
