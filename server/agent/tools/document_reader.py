"""DocumentReader —— 读取用户上传的文档，提取核心模块、功能需求和关键信息。"""
from server.agent.base_tool import BaseTool
from server.agent.tool_result import ToolResult
from server.services.llm_service import llm_service
from server.services.rag_service import rag_service
from server.utils.logger import logger


class DocumentReader(BaseTool):
    name = "document_reader"
    description = "读取用户上传的 PDF/TXT 文档，提取核心模块、功能需求、业务实体等关键信息。用于理解产品需求文档(PRD)的内容。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "document_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "要读取的文档 ID 列表",
            },
            "query": {
                "type": "string",
                "description": "针对文档的具体问题或分析方向，如'提取所有功能模块'、'找出核心业务实体'",
            },
        },
        "required": ["document_ids"],
    }

    async def execute(self, document_ids: list[int], query: str = "提取文档中的所有功能模块、业务需求和核心实体") -> ToolResult:

        try:
            # 使用 RAG 服务检索用户传入的文档内容

            all_chunks = [] # 里面的元素是“文档片段对象”
            for doc_id in document_ids:
                chunks = await rag_service.retrieve(
                    query, document_ids=[doc_id], k=10
                )
                all_chunks.extend(chunks)

            if not all_chunks:
                return ToolResult(
                    success=False,
                    error="未找到相关文档内容，请确认文档已上传并索引成功",
                )

            doc_content = "\n\n".join(
                [f"[片段 {i+1}] {c.page_content}" for i, c in enumerate(all_chunks)]
            )

            # 用 LLM 分析文档内容
            analysis_prompt = f"""你是一个产品需求分析专家。请仔细阅读以下产品需求文档（PRD）的内容，完成以下分析：

1. **核心功能模块**：列出文档中描述的所有功能模块，每个模块简要说明其职责
2. **业务实体**：识别文档中涉及的核心业务实体（如用户、订单、商品等）及其关键属性
3. **关键业务流程**：描述主要的业务流程
4. **非功能需求**：提取性能、安全、可用性等非功能需求

文档内容：
{doc_content}

⚠️ 重要规则：
- 只能提取文档中明确提及的内容，不要编造或推测文档中不存在的信息
- 文档中找不到的信息请标注「文档未提及」
- 引用内容时标注 [来源 X] 编号

请用结构化的 Markdown 格式输出分析结果。"""

            messages = [
                {"role": "system", "content": "你是一个专业的产品需求分析专家，擅长从 PRD 中提取结构化信息。你只能基于给定的文档内容进行分析，绝不编造信息。"},
                {"role": "user", "content": analysis_prompt},
            ]
            analysis = await llm_service.chat(messages, temperature=0.3, max_tokens=4096)

            return ToolResult(
                success=True,
                data={
                    "analysis": analysis,
                    "chunk_count": len(all_chunks),
                    "document_ids": document_ids,
                },
            )
        except Exception as e:
            logger.error(f"DocumentReader failed: {e}")
            return ToolResult(success=False, error=str(e))
