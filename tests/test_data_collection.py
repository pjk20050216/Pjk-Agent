"""数据采集 Agent：网页抓取、结构化落库、离线回退。"""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from harness.agents.base import build_agent_context
from harness.agents.subagents import get_agent
from harness.contracts import TaskItem
from harness.integrations.web import FetchResult
from harness.middleware.builtin import build_default_chain
from harness.tools import SUBAGENT_TOOLSETS, build_registry
from harness.tools.context import ToolContext
from harness.vfs.store import VirtualFileSystem
from tests.base import make_harness, noop_status


class DataCollectionAgentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="data-agent-"))
        self.harness, self.store, self.cfg = make_harness(self.root)
        self.run_id = f"run_data_{uuid.uuid4().hex[:8]}"
        self.vfs = VirtualFileSystem(self.run_id, self.cfg)
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
            run_id=self.run_id,
            user_id=1,
            vfs=self.vfs,
            memory=self.harness.memory,
            sandbox=self.harness.sandbox,
            skills=self.harness.skills,
            approvals=self.harness.approvals,
        )
        self.registry = await build_registry(
            self.tool_ctx, SUBAGENT_TOOLSETS["data_collection"]
        )

    def _context(self, inputs: dict):
        task = TaskItem(
            seq=1,
            title="外部数据采集",
            description="抓取公开网页并结构化",
            agent="data_collection",
            inputs=inputs,
        )
        return build_agent_context(
            run_id=self.run_id,
            objective="抓取支付风控公开新闻和监管公告",
            task=task,
            user_id=1,
            vfs=self.vfs,
            memory=self.harness.memory,
            sandbox=self.harness.sandbox,
            skills=self.harness.skills,
            tools=self.registry,
            middleware=self.chain,
            services=self.services,
            memory_context=[],
            emit=None,
            config=self.cfg,
        )

    async def test_web_fetch_to_dataset_and_report(self) -> None:
        agent = get_agent("data_collection")
        fetch = AsyncMock(
            return_value=FetchResult(
                success=True,
                url="https://example.com/news/1",
                final_url="https://example.com/news/1",
                status_code=200,
                title="监管新规",
                text="支付风控新规要求加强交易监测。",
                content_type="text/html",
                bytes_read=120,
            )
        )
        with patch("harness.tools.web.web_fetch_client.fetch", new=fetch):
            result = await agent.execute(
                self._context({"urls": ["https://example.com/news/1"]})
            )

        self.assertTrue(result.success, result.error)
        paths = {artifact.path for artifact in result.artifacts}
        self.assertIn("/workspace/data/dataset_1.json", paths)
        self.assertIn("/reports/data_collection_1.md", paths)

        payload = json.loads(await self.vfs.read("/workspace/data/dataset_1.json"))
        self.assertEqual(payload["metadata"]["mode"], "web")
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["url"], "https://example.com/news/1")
        self.assertIn("支付风控新规", payload["records"][0]["excerpt"])
        self.assertTrue(await self.vfs.exists("/logs/web/1_0.txt"))
        report = await self.vfs.read("/reports/data_collection_1.md")
        self.assertIn("数据采集报告", report)
        self.assertIn("https://example.com/news/1", report)

    async def test_offline_fallback_to_internal_rules(self) -> None:
        agent = get_agent("data_collection")
        search = AsyncMock(return_value=[])
        fetch = AsyncMock()
        old_available = None
        try:
            from harness.tools import web as web_tools

            old_available = web_tools.web_search_client._available
            web_tools.web_search_client._available = False
            with patch("harness.tools.web.web_search_client.search", new=search), patch(
                "harness.tools.web.web_fetch_client.fetch", new=fetch
            ):
                result = await agent.execute(self._context({}))
        finally:
            if old_available is not None:
                from harness.tools import web as web_tools

                web_tools.web_search_client._available = old_available

        self.assertTrue(result.success, result.error)
        payload = json.loads(await self.vfs.read("/workspace/data/dataset_1.json"))
        self.assertEqual(payload["metadata"]["mode"], "internal_rules")
        self.assertGreater(len(payload["records"]), 0)
        self.assertEqual(payload["records"][0]["source"], "internal_rules")
        fetch.assert_not_awaited()
        report = await self.vfs.read("/reports/data_collection_1.md")
        self.assertIn("回退到内部规则库", report)


if __name__ == "__main__":
    unittest.main()
