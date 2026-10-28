"""测试包初始化：强制离线模式，避免单元测试依赖外部 LLM / Milvus / Kafka。"""
import os

os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
os.environ["CUSTOM_API_KEY"] = ""
os.environ["HARNESS_MILVUS_ENABLED"] = "false"
os.environ["HARNESS_KAFKA_ENABLED"] = "false"
os.environ["HARNESS_SANDBOX_PROVIDER"] = "local"
os.environ["HARNESS_MEMORY_DISTILL"] = "true"
os.environ["HARNESS_LLM_ENABLED"] = "false"
os.environ["INTENT_GUARD_LLM_ENABLED"] = "false"
# 单元测试不访问真实搜索服务
os.environ["WEB_SEARCH_ENABLED"] = "false"

# 即使本地 .env 配置了真实密钥，单元测试也必须走确定性回退路径。
try:
    from server.llm.factory import provider_factory

    for _provider in provider_factory.list_providers():
        _provider.config.api_key = ""
except Exception:  # pragma: no cover - 导入失败时由 llm_available() 自行降级
    pass
