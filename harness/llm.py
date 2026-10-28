"""Harness 的 LLM 调用薄封装。

统一处理三件事：
1. 判断当前 provider 是否真的可用（没有 API Key 时不发请求，直接走确定性回退）；
2. 把任何 LLM 异常转成 ``None``，让上层降级而不是中断整条运行；
3. 提供 JSON 解析与 token 估算工具。
"""
from __future__ import annotations

import json
import re
from typing import Any

from harness.config import get_harness_config
from server.llm.factory import provider_factory
from server.services.llm_service import llm_service
from server.utils.logger import logger


def llm_available() -> bool:
    """当前激活 provider 是否配置了可用密钥，且 Harness 未显式关闭 LLM。"""
    try:
        if not get_harness_config().llm_enabled:
            return False
        cfg = provider_factory.get_active().config
        return bool(cfg.api_key and cfg.base_url and cfg.model)
    except Exception:
        return False


def model_name() -> str:
    try:
        return provider_factory.get_active().model
    except Exception:
        return "unknown"


async def complete(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> str | None:
    """一次补全；不可用或失败返回 None（调用方必须有确定性回退）。"""
    if not llm_available():
        return None
    try:
        return await llm_service.chat(
            messages, temperature=temperature, max_tokens=max_tokens
        )
    except Exception as exc:
        logger.warning(f"[HarnessLLM] completion failed, fallback engaged: {exc}")
        return None


def parse_json(text: str | None) -> dict | list | None:
    """从模型输出中尽力提取 JSON（容忍 ```json 围栏与前后说明文字）。"""
    if not text:
        return None
    candidates: list[str] = []
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates.extend(item.strip() for item in fenced)
    candidates.append(text.strip())
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    if "[" in text and "]" in text:
        candidates.append(text[text.index("["): text.rindex("]") + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


async def complete_json(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> Any | None:
    return parse_json(await complete(messages, temperature=temperature, max_tokens=max_tokens))


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文按 1 字 ≈ 1 token，英文按 4 字符 ≈ 1 token。"""
    if not text:
        return 0
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = max(0, len(text) - chinese)
    return chinese + other // 4 + 1


def truncate(text: str, max_chars: int, suffix: str = "…") -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - len(suffix))] + suffix
