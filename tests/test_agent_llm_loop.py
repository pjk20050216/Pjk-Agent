"""LLM 路径：Function Calling 工具循环、观察截断、结论回传。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from harness.agents.base import build_agent_context
from harness.agents.subagents import get_agent
from harness.contracts import TaskItem
from harness.middleware.builtin import build_default_chain
from harness.tools import SUBAGENT_TOOLSETS, build_registry
from harness.tools.context import ToolContext
from harness.vfs.store import VirtualFileSystem
from tests.base import make_harness, noop_status


class LLMLoopTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="llm-loop-"))
        self.harness, self.store, self.cfg = make_harness(self.root)
        self.vfs = VirtualFileSystem("run_llm", self.cfg)
        self.services = {
            "approvals": self.harness.approvals,
            "set_run_status": noop_status,
            "vfs": self.vfs,
            "memory": self.harness.memory,
            "sandbox": self.harness.sandbox,
            "skills": self.harness.skills,
            "user_role": "operator",
        }
        self.chain = build_default_chain(self.services)
        self.tool_ctx = ToolContext(
            run_id="run_llm",
            user_id=1,
            vfs=self.vfs,
            memory=self.harness.memory,
            sandbox=self.harness.sandbox,
            skills=self.harness.skills,
            approvals=self.harness.approvals,
        )
        self.task = TaskItem(
            seq=1,
            title="特征分析",
            description="分析设备风险分",
            agent="feature_analysis",
        )
        # 只给一轮工具调用：下一轮进入「无工具总结」路径，同时覆盖两条模型分支
        self.cfg.max_tool_iterations = 1

    async def test_function_calling_loop(self) -> None:
        registry = await build_registry(
            self.tool_ctx, SUBAGENT_TOOLSETS["feature_analysis"]
        )
        agent = get_agent("feature_analysis")
        context = build_agent_context(
            run_id="run_llm",
            objective="分析 R_RISK_001 误杀",
            task=self.task,
            user_id=1,
            vfs=self.vfs,
            memory=self.harness.memory,
            sandbox=self.harness.sandbox,
            skills=self.harness.skills,
            tools=registry,
            middleware=self.chain,
            services=self.services,
            memory_context=[],
            emit=None,
            config=self.cfg,
        )

        fake_tools = AsyncMock(
            side_effect=[
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "name": "feature_query",
                            "arguments": json.dumps(
                                {"feature": "device_risk_score"}, ensure_ascii=False
                            ),
                        }
                    ],
                }
            ]
        )
        fake_chat = AsyncMock(
            return_value="结论：设备风险分分布稳定\n- PSI=0.08，未超过阈值\n- 无需调整特征口径"
        )
        with patch("harness.agents.base.llm_available", return_value=True), patch(
            "harness.agents.base.llm_service.chat_with_tools", new=fake_tools
        ), patch("harness.agents.base.llm_service.chat", new=fake_chat):
            result = await agent.execute(context)

        self.assertTrue(result.success, result.error)
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "feature_query")
        self.assertIn("结论", result.summary)
        self.assertIn("PSI", result.summary)
        self.assertTrue(result.artifacts)
        self.assertGreaterEqual(fake_tools.await_count, 1)
        self.assertGreaterEqual(fake_chat.await_count, 1)


if __name__ == "__main__":
    unittest.main()
