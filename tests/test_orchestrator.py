"""编排器：确定性任务规划、依赖合法性、结果汇总。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.agents.orchestrator import Orchestrator
from harness.contracts import SubAgentResult
from harness.vfs.store import VirtualFileSystem
from tests.base import make_config


class OrchestratorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="orch-test-"))
        self.cfg = make_config(self.root)
        self.orchestrator = Orchestrator(self.cfg)

    async def test_incident_plan_covers_pipeline(self) -> None:
        plan = await self.orchestrator.plan(
            "分析 R_RISK_001 规则上线后出现的误杀问题，给出调整建议、回归测试与效果对比结论"
        )
        self.assertGreaterEqual(len(plan.tasks), 5)
        agents = [task.agent for task in plan.tasks]
        self.assertIn("anomaly_diagnosis", agents)
        self.assertIn("feature_analysis", agents)
        self.assertIn("knowledge_retrieval", agents)
        self.assertIn("rule_config", agents)
        self.assertIn("test_validation", agents)

        # 依赖必须指向已存在的、序号更小的任务
        index = {task.id: task.seq for task in plan.tasks}
        for task in plan.tasks:
            for dep in task.depends_on:
                self.assertIn(dep, index)
                self.assertLess(index[dep], task.seq)

    async def test_feature_only_plan_is_short(self) -> None:
        plan = await self.orchestrator.plan("分析 device_risk_score 的分布漂移")
        self.assertGreaterEqual(len(plan.tasks), 1)
        self.assertLessEqual(len(plan.tasks), 3)

    async def test_data_collection_only_plan(self) -> None:
        plan = await self.orchestrator.plan("抓取最近三个月支付风控公开新闻和监管公告")
        self.assertEqual([task.agent for task in plan.tasks], ["data_collection"])
        self.assertIn("数据采集", plan.strategy)

    async def test_mixed_data_and_incident_plan(self) -> None:
        plan = await self.orchestrator.plan("分析 R_RISK_001 误杀并抓取外部舆情")
        agents = [task.agent for task in plan.tasks]
        self.assertIn("data_collection", agents)
        self.assertIn("anomaly_diagnosis", agents)
        index = {task.id: task.seq for task in plan.tasks}
        for task in plan.tasks:
            for dep in task.depends_on:
                self.assertIn(dep, index)
                self.assertLess(index[dep], task.seq)

    async def test_deterministic_synthesis(self) -> None:
        plan = await self.orchestrator.plan("分析 R_RISK_001 误杀问题")
        vfs = VirtualFileSystem("run_synth", self.cfg)
        results = [
            SubAgentResult(
                task_id=plan.tasks[0].id,
                agent=plan.tasks[0].agent,
                success=True,
                summary="完成异常定位",
                findings=["误杀集中在 APP 渠道"],
                artifacts=[],
            )
        ]
        content, path = await self.orchestrator.synthesize(
            run_id="run_synth",
            objective=plan.objective,
            plan=plan,
            results=results,
            vfs=vfs,
        )
        self.assertEqual(path, "/reports/final_report_run_synth.md")
        self.assertIn("结论摘要", content)
        self.assertIn("产出资产", content)
        self.assertTrue(await vfs.exists(path))


if __name__ == "__main__":
    unittest.main()
