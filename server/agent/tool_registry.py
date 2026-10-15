"""Tool 注册中心 —— 单例模式，管理所有可用工具的注册、查找、列表。"""
from server.agent.base_tool import BaseTool
from server.utils.logger import logger


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool
        logger.info(f"Tool registered: {tool.name}")

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def list_all(self) -> list[BaseTool]:
        return list(self._tools.values())

    def get_tools_for_llm(self) -> list[dict]:
        return [t.to_openai_function() for t in self._tools.values()]


tool_registry = ToolRegistry()
