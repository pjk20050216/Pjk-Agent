"""Agent Harness 运行时能力层。

对外入口保持惰性导入，避免 ``import harness`` 触发 LLM / Redis / Milvus 连接。
"""

__all__ = ["get_harness", "AgentHarness"]


def get_harness():
    from harness.runtime import get_harness as _get

    return _get()


def __getattr__(name):
    if name == "AgentHarness":
        from harness.runtime import AgentHarness

        return AgentHarness
    raise AttributeError(name)
