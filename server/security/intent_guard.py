"""意图识别主守卫 —— 编排三层防护：正则过滤（零延迟）→ LLM 语义分类 → Prompt 护栏。

正则命中即拦截；正则未命中时按触发策略决定是否调用 LLM 语义分类（带超时 fail-open）。
"""
from dataclasses import dataclass

from server.config import settings
from server.utils.logger import logger

from server.security import regex_rules
from server.security.llm_classifier import classify

# 超长输入判定为上下文填塞的阈值（字符数）
CONTEXT_STUFFING_LENGTH_THRESHOLD = 8000

# LLM 分类置信度低于此值视为"正常"，不拦截
CLASSIFY_CONFIDENCE_THRESHOLD = 0.6


@dataclass
class GuardVerdict:
    """意图识别判定结果。"""
    blocked: bool
    category: str
    reason: str


class IntentGuard:
    """意图识别守卫：先正则、后 LLM 分类，命中即拦截。"""

    def _should_classify(self, content: str, is_first_turn: bool) -> bool:
        """按触发策略决定是否调用 LLM 分类，避免每条消息都增加首字延迟。"""
        trigger = settings.intent_guard_llm_trigger
        if trigger == "always":
            return True
        if trigger == "off":
            return False
        # 默认 first_turn_or_long：仅首轮或长输入做语义分类，命中概率低的多轮短消息跳过
        return is_first_turn or len(content) >= settings.intent_guard_llm_long_input_threshold

    async def check(self, content: str, *, is_first_turn: bool = True) -> GuardVerdict:
        if not settings.intent_guard_enabled:
            return GuardVerdict(False, "safe", "")

        # 第一层：正则快速过滤（零延迟）
        if settings.intent_guard_regex_enabled:
            if len(content) > CONTEXT_STUFFING_LENGTH_THRESHOLD:
                return GuardVerdict(True, "context_stuffing", "输入内容过长")
            hits = regex_rules.scan(content)
            if hits:
                category, matched = hits[0]
                return GuardVerdict(True, category, f"匹配到可疑模式: {matched}")

        # 第二层：LLM 语义分类（按触发策略执行，带超时，失败放行靠 Prompt 护栏兜底）
        if settings.intent_guard_llm_enabled:
            if not self._should_classify(content, is_first_turn):
                logger.debug("[IntentGuard] LLM classify skipped by trigger policy")
                return GuardVerdict(False, "safe", "")
            try:
                result = await classify(content, settings.intent_guard_llm_timeout_ms)
                category = result.get("category", "safe")
                confidence = float(result.get("confidence", 0.0))
                if category != "safe" and confidence >= CLASSIFY_CONFIDENCE_THRESHOLD:
                    return GuardVerdict(True, category, result.get("reason", ""))
            except Exception as e:
                logger.warning(f"[IntentGuard] LLM classification failed (fail-open): {e}")

        return GuardVerdict(False, "safe", "")


# 全局单例守卫
intent_guard = IntentGuard()
