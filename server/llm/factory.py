"""LLM Provider 工厂 —— 解析配置、懒创建适配器、管理激活提供商与运行时切换。"""
import os

from server.config import LLM_PROVIDERS, LLM_ACTIVE_PROVIDER
from server.llm.base import ProviderConfig
from server.llm.openai_compat import OpenAICompatProvider
from server.utils.logger import logger


class LLMProviderFactory:
    """工厂：把 YAML 里的 providers 列表解析成适配器，按需懒创建并管理激活索引。"""

    def __init__(self):
        self._configs: list[ProviderConfig] = self._build_configs()
        self._providers: dict[str, OpenAICompatProvider] = {}
        if 0 <= LLM_ACTIVE_PROVIDER < len(self._configs):
            self._active_index = LLM_ACTIVE_PROVIDER
        else:
            self._active_index = 0

    @staticmethod
    def _build_configs() -> list[ProviderConfig]:
        configs = []
        for p in LLM_PROVIDERS:
            base_url = p.get("base_url", "")
            api_key_env = p.get("api_key_env", "")
            configs.append(
                ProviderConfig(
                    name=p.get("name", ""),
                    base_url=base_url,
                    api_key_env=api_key_env,
                    api_key=os.environ.get(api_key_env, ""),
                    model=p.get("default_model", ""),
                    embedding_model=p.get("embedding_model", ""),
                    embedding_base_url=p.get("embedding_base_url", base_url),
                    classifier_model=p.get("classifier_model", ""),
                    embedding_check_ctx_length=bool(
                        p.get("embedding_check_ctx_length", False)
                    ),
                )
            )
        return configs

    def get(self, name: str) -> OpenAICompatProvider:
        """按名称获取适配器（不存在则懒创建）。"""
        if name not in self._providers:
            cfg = next((c for c in self._configs if c.name == name), None)
            if cfg is None:
                raise KeyError(f"Unknown LLM provider: {name}")
            self._providers[name] = OpenAICompatProvider(cfg)
        return self._providers[name]

    def get_active(self) -> OpenAICompatProvider:
        """返回当前激活的适配器。"""
        if not self._configs:
            raise RuntimeError("No LLM providers configured")
        return self.get(self._configs[self._active_index].name)

    def set_active(self, index_or_name) -> OpenAICompatProvider:
        """切换激活提供商（按索引或名称），返回切换后的适配器。"""
        old_embedding = self.get_active().config.embedding_model if self._configs else None

        if isinstance(index_or_name, int):
            if not 0 <= index_or_name < len(self._configs):
                raise IndexError(f"Provider index out of range: {index_or_name}")
            self._active_index = index_or_name
        else:
            for i, cfg in enumerate(self._configs):
                if cfg.name == index_or_name:
                    self._active_index = i
                    break
            else:
                raise KeyError(f"Unknown LLM provider: {index_or_name}")

        active = self.get_active()
        logger.info(f"[LLMFactory] Active provider switched to: {active.name}")

        if old_embedding and old_embedding != active.config.embedding_model:
            self._schedule_reindex()

        return active

    def _schedule_reindex(self):
        """embedding 模型变化时，后台线程自动重建向量索引。"""
        import threading

        def _run():
            from server.database import SessionLocal
            from server.services.rag_service import rag_service

            db = SessionLocal()
            try:
                n = rag_service.rebuild_index(db)
                logger.info(f"[LLMFactory] Auto reindex completed: {n} documents")
            except Exception as e:
                logger.error(f"[LLMFactory] Auto reindex failed: {e}")
            finally:
                db.close()

        logger.info("[LLMFactory] Embedding model changed, scheduling auto reindex")
        threading.Thread(target=_run, daemon=True).start()

    def list_providers(self) -> list[OpenAICompatProvider]:
        """返回所有适配器（按配置顺序）。"""
        return [self.get(cfg.name) for cfg in self._configs]


# 全局单例工厂
provider_factory = LLMProviderFactory()
