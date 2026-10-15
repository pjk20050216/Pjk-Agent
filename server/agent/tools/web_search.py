"""WebSearchTool —— 联网搜索互联网最新信息，返回带 URL 来源的结果并做要点提炼。"""
from server.agent.base_tool import BaseTool
from server.agent.tool_result import ToolResult
from server.services.llm_service import llm_service
from server.utils.web_search import web_search_client
from server.utils.logger import logger


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "联网搜索互联网上的最新公开信息，用于回答知识库之外、需要实时或外部资料的问题。返回带 URL 来源的结果。"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词或问题",
            },
            "count": {
                "type": "integer",
                "description": "返回结果条数，默认 5",
            },
            "freshness": {
                "type": "string",
                "description": "时间范围：noLimit（不限，默认）/ oneDay / oneWeek / oneMonth / oneYear",
            },
        },
        "required": ["query"],
    }

    async def execute(self, query: str, count: int = 5, freshness: str = "noLimit") -> ToolResult:
        try:
            results = await web_search_client.search(query, count=count, freshness=freshness)
            if not results:
                return ToolResult(
                    success=False,
                    error="联网搜索未返回结果（服务不可用或查询无匹配）",
                )

            sources = "\n\n".join(
                f"【来源 {i + 1}】{r['title']}\nURL: {r['url']}\n"
                f"{(r.get('summary') or r.get('snippet') or '').strip()}"
                for i, r in enumerate(results)
            )

            prompt = f"""请基于以下联网搜索结果，提炼与问题相关的关键信息，形成结构化的要点回答。

用户问题：{query}

搜索结果：
{sources}

⚠️ 重要规则：
- 只能基于上述搜索结果作答，不要编造搜索中不存在的内容
- 引用信息时标注对应的【来源 N】编号，并保留关键 URL
- 若结果不足以回答问题，请明确说明

请用简洁的 Markdown 输出。"""

            messages = [
                {"role": "system", "content": "你是一个联网信息分析助手，擅长从搜索结果中提炼准确的要点，且只依据给定结果作答。"},
                {"role": "user", "content": prompt},
            ]
            summary = await llm_service.chat(messages, temperature=0.3, max_tokens=4096)

            return ToolResult(
                success=True,
                data={"query": query, "results": results, "summary": summary},
            )
        except Exception as e:
            logger.error(f"WebSearchTool failed: {e}")
            return ToolResult(success=False, error=str(e))
