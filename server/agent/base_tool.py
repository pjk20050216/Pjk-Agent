from abc import ABC, abstractmethod
from server.agent.tool_result import ToolResult
"""
Tool 抽象基类 —— 所有工具必须继承此类并实现 execute 方法。
execute：为整个系统提供了一个统一的工具执行接口，上层调用者无需关心具体工具的实现细节，只需以统一的方式调用 execute 并处理标准化的返回结果即可。
"""


class BaseTool(ABC):
    name: str
    description: str
    # 这个是用于定义模型需要知道工具要“接收哪些参数、参数的类型是什么、哪些是必填项”
    parameters_schema: dict

    # 凡是继承了该抽象基类的子类,必须实现被 @abstractmethod 修饰的方法
    @abstractmethod
    # Spring Boot 中的 CompletableFuture 等价 async 异步特性
    async def execute(self, **kwargs) -> ToolResult:
        ...

    # 将工具转换为 OpenAI 的 Function 格式，用于打包发给大模型
    def to_openai_function(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }
