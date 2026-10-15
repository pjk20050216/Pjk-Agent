"""
聊天业务逻辑 —— 构建消息上下文（系统提示词 + 历史 + RAG），调用 LLM 流式生成并返回 SSE 响应。
负责组装上下文、调用大模型，实现流式输出
"""
from sqlalchemy.orm import Session
from fastapi import HTTPException
from server.config import settings
from server.services.llm_service import llm_service
from server.services.conversation_service import save_message, get_conversation, get_messages
from server.security import intent_guard
from server.utils.logger import logger
from server.utils.sse import sse_event, sse_response
from server.utils.web_search import web_search_client

def _load_prompt_file() -> str:
    """加载 prompts/system_prompt.txt 作为系统提示词。"""
    from pathlib import Path
    prompt_file = Path(__file__).parent.parent.parent / "prompts" / "system_prompt.txt"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8").strip()
    return "You are a helpful, intelligent AI assistant. Respond in the user's language."


def _load_guard_rail() -> str:
    """加载 prompts/guard_rail.txt 作为安全护栏，追加到系统提示词末尾。"""
    from pathlib import Path
    guard_file = Path(__file__).parent.parent.parent / "prompts" / "guard_rail.txt"
    if guard_file.exists():
        return guard_file.read_text(encoding="utf-8").strip()
    return ""


RAG_CONTEXT_TEMPLATE = """## 用户上传的文档检索结果（仅限以下带来源标记的片段用于回答）

以下是用户上传的文档中与问题最相关的片段。每个片段标注了 [来源 X] 编号。
**只能基于以下片段回答，文档中找不到的信息请明确说「文档中未提及相关内容」。**

{rag_context}

---
请根据以上文档片段回答用户的问题。引用文档内容时必须标注 [来源 X]。"""


WEB_CONTEXT_TEMPLATE = """## 联网搜索结果（可引用，需标注来源）

以下是针对用户问题的实时联网搜索结果，每条标注了【网络来源 X】编号与 URL。
可以基于这些结果回答，引用时请标注对应的【网络来源 X】并给出关键 URL。
若结果与问题无关或不足以回答，请如实说明，不要编造。

{web_context}

---"""


def _build_messages(history: list[dict], rag_context: str = "", web_context: str = "") -> list[dict]:
    """构建 messages 列表：系统提示词（+RAG 上下文 + 联网上下文）+ 历史对话。"""
    system_prompt = _load_prompt_file()
    if rag_context:
        system_prompt += "\n\n" + RAG_CONTEXT_TEMPLATE.format(rag_context=rag_context)
    if web_context:
        system_prompt += "\n\n" + WEB_CONTEXT_TEMPLATE.format(web_context=web_context)
    system_prompt += "\n\n" + _load_guard_rail()

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    return messages


async def stream_chat_response(
    db: Session,
    conversation_id: int,
    user_id: int,
    content: str,
    document_ids: list[int] | None = None,
    enable_web_search: bool | None = None,
):
    """核心聊天流程：验证会话归属 → 保存用户消息 → 检索 RAG / 联网 → 流式返回 LLM 回复（SSE）。"""
    # Verify conversation belongs to user
    get_conversation(db, conversation_id, user_id)

    # 是否为对话首轮（LLM 分类触发策略用；此时用户消息尚未保存）
    is_first_turn = len(get_messages(db, conversation_id, user_id)) == 0

    # 意图识别：命中即拦截（在保存用户消息之前，避免污染对话历史）
    verdict = await intent_guard.check(content, is_first_turn=is_first_turn)
    if verdict.blocked:
        logger.warning(
            f"[IntentGuard] blocked: category={verdict.category} user_id={user_id} "
            f"reason={verdict.reason} content={content[:50]!r}"
        )
        raise HTTPException(status_code=403, detail=f"请求被拒绝：{verdict.reason}")

    # Save user message
    save_message(db, conversation_id, "user", content)

    # Get conversation history
    msgs = get_messages(db, conversation_id, user_id)
    history = [{"role": m.role, "content": m.content} for m in msgs]

    # Build RAG context if documents provided
    rag_context = ""
    if document_ids:
        try:
            from server.services.rag_service import rag_service
            chunks = await rag_service.retrieve(content, document_ids=document_ids)
            if chunks:
                rag_context = "\n\n".join([c.page_content for c in chunks])
            else:
                rag_context = "（本次检索未找到与用户问题足够相关的文档片段。）"
        except Exception as e:
            logger.warning(f"RAG retrieval failed: {e}")
            rag_context = "（文档检索暂时不可用。）"

    # Build web search context if enabled
    web_context = ""
    if enable_web_search:
        try:
            results = await web_search_client.search(content, count=settings.web_search_default_count)
            if results:
                web_context = "\n\n".join(
                    f"【网络来源 {i + 1}】{r['title']}（{r['url']}）\n"
                    f"{(r.get('summary') or r.get('snippet') or '').strip()}"
                    for i, r in enumerate(results)
                )
            else:
                web_context = "（本次联网检索未找到相关结果。）"
        except Exception as e:
            logger.warning(f"Web search failed: {e}")
            web_context = "（联网检索暂时不可用。）"

    # 加载系统提示词，如果有 RAG / 联网提示词的话，则加载到系统提示词末尾
    messages = _build_messages(history, rag_context, web_context)

    async def event_generator():
        """SSE 事件生成器：逐 token 发送 'token' 事件，完成后发送 'done' 事件，异常时发送 'error' 事件。"""
        full_response = ""
        try:
            # 调用 LLM 进行流式生成
            async for token in llm_service.chat_stream(messages):
                full_response += token
                yield sse_event("token", {"content": token})

            # Save assistant message
            msg = save_message(db, conversation_id, "assistant", full_response)
            # Update conversation updated_at
            conv = get_conversation(db, conversation_id, user_id)
            from datetime import datetime, timezone
            conv.updated_at = datetime.now(timezone.utc)
            db.commit()

            yield sse_event("done", {"message_id": msg.id, "conversation_id": conversation_id})
        except Exception as e:
            logger.error(f"Stream error: {e}")
            if full_response:
                save_message(db, conversation_id, "assistant", full_response + f"\n\n[Error: {e}]")
            yield sse_event("error", {"detail": str(e)})

    return sse_response(event_generator())
