"""工具协议与注册中心。

与 LangChain Tool 不同，这里刻意保持轻量：
- 工具是纯 async 函数 + JSON Schema 描述；
- 风险等级 / 审批动作随工具声明，中间件据此拦截；
- 注册表按运行实例化（VFS、记忆、沙箱都是 run 级资源）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from harness.contracts import RiskLevel, ToolSpec


@dataclass
class HarnessTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    risk: RiskLevel = RiskLevel.LOW
    action: str = "tool.invoke"
    tags: list[str] = field(default_factory=list)
    cacheable: bool = False
    requires_approval: bool = False

    async def run(self, **kwargs: Any) -> Any:
        return await self.handler(**kwargs)

    def to_openai_function(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            risk=self.risk,
            action=self.action,
            tags=self.tags,
            requires_approval=self.requires_approval,
        )


class ToolRegistry:
    def __init__(self, tools: list[HarnessTool] | None = None) -> None:
        self._tools: dict[str, HarnessTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: HarnessTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> HarnessTool | None:
        return self._tools.get(name)

    def list(self) -> list[HarnessTool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_tools(self, allow: set[str] | None = None) -> list[dict]:
        return [
            tool.to_openai_function()
            for tool in self._tools.values()
            if allow is None or tool.name in allow
        ]

    def specs(self) -> list[ToolSpec]:
        return [tool.spec() for tool in self._tools.values()]

    def subset(self, names: list[str] | set[str]) -> "ToolRegistry":
        return ToolRegistry([t for t in self._tools.values() if t.name in names])


def schema(
    properties: dict[str, dict],
    required: list[str] | None = None,
) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


STR = {"type": "string"}
INT = {"type": "integer"}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}
