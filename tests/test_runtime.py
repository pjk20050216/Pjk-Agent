"""运行时端到端：离线多智能体执行、事件、追踪、记忆沉淀、恢复语义。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.contracts import RunStatus, TaskStatus
from harness.vfs.store import VirtualFileSystem
from tests.base import make_harness


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="runtime-test-"))
        self.harness, self.store, self.cfg = make_harness(self.root)

    async def asyncTearDown(self) -> None:
        await self.harness.shutdown()

    async def test_offline_run_completes(self) -> None:
        snapshot = await self.harness.create_run(
            "分析 R_RISK_001 规则上线后的误杀问题，给出调整建议、回归测试与效果对比结论",
            user_id=1,
        )
        await self.harness.start(snapshot.id)
        result = await self.harness.wait(snapshot.id, timeout=60)

        self.assertIsNotNone(result)
        self.assertEqual(result.status, RunStatus.COMPLETED.value)
        self.assertIsNotNone(result.plan)
        self.assertEqual(len(result.plan.tasks), 6)
        for task in result.plan.tasks:
            self.assertEqual(task.status, TaskStatus.COMPLETED.value, task.title)
            self.assertTrue(task.artifacts, task.title)

        self.assertIsNotNone(result.result)
        report_path = result.result["report_path"]
        self.assertTrue(report_path.startswith("/reports/"))
        self.assertGreater(len(result.result["summary"]), 100)

        events = self.harness.bus.read_events(snapshot.id)
        types = {event.type for event in events}
        self.assertIn("run.created", types)
        self.assertIn("plan.created", types)
        self.assertIn("task.completed", types)
        self.assertIn("run.completed", types)

        spans = self.harness.recorder.read_spans(snapshot.id)
        self.assertGreaterEqual(len(spans), 5)

        # 报告与版本留痕真实落盘
        vfs_path = self.cfg.workspace_root / "runs" / snapshot.id / "fs" / report_path.lstrip("/")
        self.assertTrue(vfs_path.exists())
        history = await self.harness.memory.recall("误杀 规则", user_id=1, top_k=5)
        self.assertTrue(history, "run 结束后应沉淀长期经验")

    async def test_completed_run_is_not_restarted(self) -> None:
        snapshot = await self.harness.create_run("分析 R_RISK_001 误杀", user_id=1)
        await self.harness.start(snapshot.id)
        first = await self.harness.wait(snapshot.id, timeout=60)
        self.assertEqual(first.status, RunStatus.COMPLETED.value)

        resumed = await self.harness.resume(snapshot.id)
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.status, RunStatus.COMPLETED.value)
        self.assertFalse(self.harness.is_running(snapshot.id))

    async def test_data_collection_run_falls_back_offline(self) -> None:
        snapshot = await self.harness.create_run(
            "采集支付风控公开新闻和监管公告", user_id=1
        )
        await self.harness.start(snapshot.id)
        result = await self.harness.wait(snapshot.id, timeout=60)

        self.assertIsNotNone(result)
        self.assertEqual(result.status, RunStatus.COMPLETED.value)
        self.assertIsNotNone(result.plan)
        self.assertEqual(len(result.plan.tasks), 1)
        self.assertEqual(result.plan.tasks[0].agent, "data_collection")
        self.assertTrue(result.plan.tasks[0].artifacts)
        dataset = [
            path
            for path in result.plan.tasks[0].artifacts
            if path.startswith("/workspace/data/")
        ]
        self.assertTrue(dataset, result.plan.tasks[0].artifacts)
        vfs = VirtualFileSystem(snapshot.id, self.cfg)
        payload = json.loads(await vfs.read(dataset[0]))
        self.assertEqual(payload["metadata"]["mode"], "internal_rules")
        self.assertGreater(len(payload["records"]), 0)

    async def test_unknown_run(self) -> None:
        self.assertIsNone(await self.harness.get("run_missing"))
        with self.assertRaises(KeyError):
            await self.harness.start("run_missing")


if __name__ == "__main__":
    unittest.main()
