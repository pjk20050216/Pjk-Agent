"""模型路由 —— 前端获取可用 LLM 提供商列表，以及运行时切换激活提供商。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from server.config import settings
from server.dependencies import get_current_user
from server.llm import provider_factory
from server.models.user import User
from server.utils.cache import get_cached_route, set_cached_route
from server.utils.redis_client import redis_client
import yaml
from pathlib import Path

router = APIRouter(prefix="/api/models", tags=["models"])

_models_cache_key = "devflow:route:models"

_config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"


def _load_providers():
    """从 config/settings.yaml 读取所有 LLM 提供商配置。"""
    if _config_path.exists():
        with open(_config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        llm = data.get("llm", {})
        return llm.get("providers", [])
    return []


@router.get("")
def list_models():
    """GET /api/models —— 返回所有提供商名称、默认模型和当前激活的模型。"""
    cached = get_cached_route(_models_cache_key)
    if cached:
        return cached

    active = provider_factory.get_active()
    providers = _load_providers()
    result = {
        "providers": [
            {
                "name": p["name"],
                "default_model": p.get("default_model", ""),
            }
            for p in providers
        ],
        "active_provider": active.name,
        "current_model": active.model,
    }
    set_cached_route(_models_cache_key, result, settings.redis_ttl_route_static)
    return result


class SwitchProviderRequest(BaseModel):
    """切换目标：提供商名称（与 /api/models 返回的 name 一致）。"""

    provider: str = Field(..., min_length=1)


@router.post("/switch")
def switch_model(
    req: SwitchProviderRequest,
    current_user: User = Depends(get_current_user),
):
    """POST /api/models/switch —— 切换全局激活的 LLM 提供商。

    切换由进程内单例生效、对所有用户可见；若新提供商的 embedding 模型与旧的不同，
    工厂会在后台自动重建向量索引（重建完成前 RAG 检索可能返回空结果）。
    """
    old_embedding = provider_factory.get_active().config.embedding_model
    try:
        active = provider_factory.set_active(req.provider)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {req.provider}")

    reindex_triggered = old_embedding != active.config.embedding_model

    try:
        redis_client.delete_sync(_models_cache_key)
    except Exception:
        pass

    return {
        "active_provider": active.name,
        "model": active.model,
        "embedding_model": active.config.embedding_model,
        "reindex_triggered": reindex_triggered,
        "detail": (
            "Provider switched, vector index rebuild scheduled in background"
            if reindex_triggered
            else "Provider switched"
        ),
    }
