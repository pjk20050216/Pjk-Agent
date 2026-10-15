"""LLM Provider 适配器接口 —— 抽象基类 + 配置数据类。

适配器模式：将不同 LLM 提供商（DashScope / OpenAI / 自定义 OpenAI 兼容）
统一到同一套对话接口，供工厂按需实例化与切换。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class ProviderConfig:
    """单个 LLM 提供商的配置快照。"""
    name: str
    base_url: str
    api_key_env: str
    api_key: str
    model: str
    embedding_model: str
    embedding_base_url: str
    # 意图识别专用的小/快模型；为空则回退到 model
    classifier_model: str = ""
    # 是否让 langchain 先做 tiktoken 分词、再以「整数 token 数组」调用
    # /embeddings。OpenAI 官方接受该形式，但 DashScope 兼容模式会拒绝：
    #   400 InvalidParameter: contents is neither str nor list of str.
    # 故默认 False（直接发送原始文本），兼容两者。
    embedding_check_ctx_length: bool = False


class BaseLLMProvider(ABC):
    """LLM 提供商适配器接口。

    每个具体适配器封装自己的客户端、熔断器与调用逻辑，
    对外暴露统一的 4 个对话方法。
    """

    def __init__(self, config: ProviderConfig):
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def model(self) -> str:
        return self.config.model

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        model: str | None = None,
    ) -> str:
        """一次性聊天补全，返回完整文本。model 为 None 时使用配置的默认对话模型。"""

    @abstractmethod
    async def chat_stream(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 4096) -> AsyncIterator[str]:
        """流式聊天补全，逐 token yield。"""

    @abstractmethod
    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict:
        """支持 Function Calling 的聊天补全。"""

    @abstractmethod
    async def chat_stream_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> AsyncIterator[dict]:
        """流式 + Function Calling，逐 token yield 后 yield tool_calls 事件。"""
