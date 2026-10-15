"""
全局配置模块 —— 从 config/settings.yaml 加载 LLM 提供商列表，
结合环境变量提供 JWT、数据库、向量存储、文件上传等配置。
"""
import hashlib
import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic_settings import BaseSettings
import yaml

PROJECT_ROOT = Path(__file__).parent.parent

# 必须放在任何 _get_env()/os.environ 读取之前执行：
# 本模块大量通过 os.environ 直读密钥（DASHSCOPE_API_KEY / BOCHA_API_KEY 等），
# 而 pydantic-settings 的 env_file 只会按「字段名大写」匹配（LLM_API_KEY），
# 命名不一致的键不会被它带进 os.environ。override=False 保证真实环境变量优先于 .env。
load_dotenv(PROJECT_ROOT / ".env")

def _load_yaml_settings() -> dict:
    """加载 config/settings.yaml 中的 YAML 配置（LLM 提供商列表等）。"""
    yaml_path = PROJECT_ROOT / "config" / "settings.yaml"
    if yaml_path.exists():
        with open(yaml_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}

_yaml = _load_yaml_settings()
_llm = _yaml.get("llm", {})
_active_provider = _llm.get("active_provider", 0)
_providers = _llm.get("providers", [])
_redis = _yaml.get("redis", {})
_redis_ttl = _redis.get("ttl", {})
_gateway = _yaml.get("gateway", {})
_rate_limit = _yaml.get("rate_limit", {})
_circuit_breaker = _yaml.get("circuit_breaker", {})
_intent_guard = _yaml.get("intent_guard", {})
_es = _yaml.get("elasticsearch", {})
_web_search = _yaml.get("web_search", {})

if _providers and 0 <= _active_provider < len(_providers):
    _provider = _providers[_active_provider]
else:
    _provider = {}

# 供 LLM 适配器工厂使用：原始 provider 配置列表 + 当前激活索引
LLM_PROVIDERS = _providers
LLM_ACTIVE_PROVIDER = _active_provider

def _get_env(key: str, default: str = "") -> str:
    """从系统环境变量读取配置值。"""
    return os.environ.get(key, default)

# 这里使用到了 Pydantic 中的 BaseSettings 类，它允许我们通过环境变量来覆盖配置（也就是下面中获取环境变量中的 api-key）
class Settings(BaseSettings):
    """应用全局配置，包含 JWT、数据库、LLM、Chroma 向量存储、文件上传等参数。"""

    # JWT 认证配置
    jwt_secret_key: str = _get_env("JWT_SECRET_KEY", "change-me-in-production")  # JWT 密钥，默认值为"change-me-in-production"
    jwt_algorithm: str = "HS256"  # JWT 加密算法，使用 HS256
    access_token_expire_minutes: int = 30  # 访问令牌过期时间，单位为分钟
    refresh_token_expire_days: int = 7  # 刷新令牌过期时间，单位为天

    # SQLite 数据库路径
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'app.db'}"  # SQLite 数据库 URL 路径

    # LLM 对话模型配置
    llm_base_url: str = _provider.get("base_url", "https://api.openai.com/v1")  # LLM 服务基础 URL，默认为 OpenAI API 地址

    # _get_env() 调用 os.environ.get(key)，而 pydantic-settings 的 BaseSettings 内部通过 class Config: env_file = ".env" 自动把 .env 文件内容加载进 os.environ，
    # 所以最终密钥被读到 settings.llm_api_key中。
    llm_api_key: str = _get_env(_provider.get("api_key_env", "OPENAI_API_KEY"))  # LLM API 密钥，从环境变量获取
    llm_model: str = _provider.get("default_model", "gpt-4o")  # LLM 模型名称，默认为 gpt-4o

    # Embedding 模型配置（用于 RAG 文档向量化）
    embedding_base_url: str = _provider.get("embedding_base_url", llm_base_url)  # Embedding 模型服务基础 URL，默认使用 LLM 的基础 URL
    embedding_model: str = _provider.get("embedding_model", "text-embedding-3-small")  # Embedding 模型名称，默认为 text-embedding-3-small

    # Chroma 向量数据库配置（RAG 文档检索）
    chroma_persist_dir: str = str(PROJECT_ROOT / "chroma_db")
    chroma_collection_name: str = "user_documents"
    rag_relevance_threshold: float = float(_get_env("RAG_RELEVANCE_THRESHOLD", "0.45"))
    rag_top_k: int = int(_get_env("RAG_TOP_K", "6"))

    # Elasticsearch 全文检索（与 Chroma 混合检索）
    es_enabled: bool = _get_env("ES_ENABLED", str(_es.get("enabled", True))).lower() == "true"
    es_url: str = _get_env("ES_URL", _es.get("url", "http://localhost:9200"))
    es_index: str = _get_env("ES_INDEX", _es.get("index", "devflow_documents"))
    es_username: str = _get_env("ES_USERNAME", _es.get("username", ""))
    es_password: str = _get_env("ES_PASSWORD", _es.get("password", ""))
    rag_hybrid_enabled: bool = _get_env(
        "RAG_HYBRID_ENABLED", str(_es.get("hybrid_enabled", True))
    ).lower() == "true"
    rag_es_top_k: int = int(_get_env("RAG_ES_TOP_K", str(_es.get("es_top_k", 6))))
    rag_rrf_k: int = int(_get_env("RAG_RRF_K", str(_es.get("rrf_k", 60))))

    # 联网搜索（博查 Bocha）
    web_search_enabled: bool = _get_env(
        "WEB_SEARCH_ENABLED", str(_web_search.get("enabled", True))
    ).lower() == "true"
    web_search_api_key: str = _get_env("BOCHA_API_KEY", _web_search.get("api_key", ""))
    web_search_base_url: str = _get_env(
        "WEB_SEARCH_BASE_URL", _web_search.get("base_url", "https://api.bochaai.com/v1/web-search")
    )
    web_search_default_count: int = int(
        _get_env("WEB_SEARCH_COUNT", str(_web_search.get("count", 5)))
    )
    web_search_timeout: int = int(
        _get_env("WEB_SEARCH_TIMEOUT", str(_web_search.get("timeout", 15)))
    )

    # Redis 缓存配置
    redis_host: str = _get_env("REDIS_HOST", _redis.get("host", "localhost"))
    redis_port: int = int(_get_env("REDIS_PORT", str(_redis.get("port", 6379))))
    redis_db: int = int(_get_env("REDIS_DB", str(_redis.get("db", 0))))
    redis_password: str = _get_env("REDIS_PASSWORD", _redis.get("password", ""))
    redis_ttl_llm: int = int(_get_env("REDIS_TTL_LLM", str(_redis_ttl.get("llm", 3600))))
    redis_ttl_embedding: int = int(_get_env("REDIS_TTL_EMBEDDING", str(_redis_ttl.get("embedding", 3600))))
    redis_ttl_rag: int = int(_get_env("REDIS_TTL_RAG", str(_redis_ttl.get("rag", 1800))))
    redis_ttl_tool: int = int(_get_env("REDIS_TTL_TOOL", str(_redis_ttl.get("tool", 3600))))
    redis_ttl_route_static: int = int(_get_env("REDIS_TTL_ROUTE_STATIC", str(_redis_ttl.get("route_static", 3600))))
    redis_ttl_route_user: int = int(_get_env("REDIS_TTL_ROUTE_USER", str(_redis_ttl.get("route_user", 30))))
    redis_ttl_websearch: int = int(_get_env("REDIS_TTL_WEBSEARCH", str(_redis_ttl.get("websearch", 900))))

    # 文件上传限制
    upload_dir: str = str(PROJECT_ROOT / "data" / "uploads")
    max_upload_size_mb: int = 20
    allowed_file_types: list[str] = ["pdf", "txt"]

    # 部署模式：是否运行在网关之后
    behind_gateway: bool = _get_env("BEHIND_GATEWAY", str(_gateway.get("enabled", True))).lower() == "true"

    # 网关内部信任令牌：网关转发时携带，后端仅凭「请求来自 localhost」不足以信任 X-User-ID
    # （任何本机进程都能连 127.0.0.1），故需共享密钥二次确认。留空则从 JWT 密钥派生，
    # 保证网关与后端两个进程无需额外配置即可算出一致值。
    gateway_internal_token: str = _get_env("GATEWAY_INTERNAL_TOKEN", "")

    @property
    def internal_token(self) -> str:
        """实际用于网关↔后端互认的令牌（未显式配置时由 JWT 密钥派生）。"""
        if self.gateway_internal_token:
            return self.gateway_internal_token
        return hashlib.sha256(
            f"devflow-gateway-internal:{self.jwt_secret_key}".encode("utf-8")
        ).hexdigest()

    # 熔断器配置
    circuit_breaker_enabled: bool = True
    circuit_breaker_threshold: int = int(
        _get_env("CIRCUIT_BREAKER_THRESHOLD", str(_circuit_breaker.get("failure_threshold", 5)))
    )
    circuit_breaker_timeout: int = int(
        _get_env("CIRCUIT_BREAKER_TIMEOUT", str(_circuit_breaker.get("recovery_timeout", 60)))
    )

    # 速率限制（供网关使用，此处集中定义）
    rate_limit_authenticated: str = _get_env(
        "RATE_LIMIT_AUTH", _rate_limit.get("authenticated", "100/minute")
    )
    rate_limit_unauthenticated: str = _get_env(
        "RATE_LIMIT_UNAUTH", _rate_limit.get("unauthenticated", "30/minute")
    )
    llm_concurrency_limit: int = int(
        _get_env("LLM_CONCURRENCY_LIMIT", str(_rate_limit.get("llm_concurrency", 5)))
    )

    # 意图识别（防注入）配置
    intent_guard_enabled: bool = _get_env(
        "INTENT_GUARD_ENABLED", str(_intent_guard.get("enabled", True))
    ).lower() == "true"
    intent_guard_regex_enabled: bool = _get_env(
        "INTENT_GUARD_REGEX_ENABLED", str(_intent_guard.get("regex_enabled", True))
    ).lower() == "true"
    intent_guard_llm_enabled: bool = _get_env(
        "INTENT_GUARD_LLM_ENABLED", str(_intent_guard.get("llm_enabled", True))
    ).lower() == "true"
    intent_guard_llm_timeout_ms: int = int(
        _get_env("INTENT_GUARD_LLM_TIMEOUT_MS", str(_intent_guard.get("llm_timeout_ms", 1500)))
    )
    # LLM 分类触发策略：always（每条）| first_turn_or_long（默认）| off（不调用）
    intent_guard_llm_trigger: str = _get_env(
        "INTENT_GUARD_LLM_TRIGGER", _intent_guard.get("llm_trigger", "first_turn_or_long")
    )
    # first_turn_or_long 策略下，超过该字符数的输入也触发 LLM 分类
    intent_guard_llm_long_input_threshold: int = int(
        _get_env(
            "INTENT_GUARD_LLM_LONG_INPUT_THRESHOLD",
            str(_intent_guard.get("llm_long_input_threshold", 200)),
        )
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


# 全局单例配置对象，供整个应用使用
settings = Settings()
