"""把 Java 侧工具清单包装成 HarnessTool。"""
from __future__ import annotations

from typing import Any

from harness.contracts import RiskLevel
from harness.integrations.java_bridge import JavaToolBridge
from harness.tools.base import HarnessTool
from harness.tools.context import ToolContext


def _risk(value: Any) -> RiskLevel:
    try:
        return RiskLevel(str(value))
    except ValueError:
        return RiskLevel.MEDIUM


async def build_java_tools(ctx: ToolContext, bridge: JavaToolBridge) -> list[HarnessTool]:
    if not bridge.enabled:
        return []
    manifest = await bridge.list_tools()
    tools: list[HarnessTool] = []
    for item in manifest:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        parameters = item.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}, "additionalProperties": False}
        description = str(item.get("description", f"Java tool {name}"))
        action = str(item.get("action", "java.tool.invoke"))
        risk = _risk(item.get("risk", "medium"))
        requires = bool(item.get("requires_approval", False))

        async def _handler(_name: str = name, **kwargs: Any) -> dict:
            return await bridge.invoke(
                _name, kwargs, run_id=ctx.run_id, user_id=ctx.user_id
            )

        tools.append(
            HarnessTool(
                name=name,
                description=description,
                parameters=parameters,
                handler=_handler,
                risk=risk,
                action=action,
                tags=["java", *[str(t) for t in item.get("tags", [])]],
                requires_approval=requires,
            )
        )
    return tools
