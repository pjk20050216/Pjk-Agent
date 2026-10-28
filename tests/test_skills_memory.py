"""Skills 渐进式披露与长期记忆检索 / 蒸馏。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.contracts import MemoryRecord, RunSnapshot, SubAgentResult, TaskItem, TaskPlan
from harness.memory.distillation import distill_run
from harness.memory.manager import MemoryManager
from harness.skills.registry import SkillRegistry
from tests.base import make_config


class SkillsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="skill-test-"))
        self.cfg = make_config(self.root)
        self.skills = SkillRegistry(self.cfg.skills_dir)

    def test_catalog_loaded(self) -> None:
        self.assertGreaterEqual(len(self.skills), 5)
        names = {meta.name for meta in self.skills.list_meta()}
        self.assertIn("rule-review-sop", names)
        self.assertIn("incident-triage", names)

    def test_progressive_disclosure(self) -> None:
        catalog = self.skills.catalog_prompt()
        self.assertIn("rule-review-sop", catalog)
        body = self.skills.load_body("rule-review-sop")
        self.assertIn("执行步骤", body)
        resource = self.skills.load_resource(
            "rule-review-sop", "templates/rule_review_report.md"
        )
        self.assertIn("规则评审报告模板", resource)

    def test_match(self) -> None:
        matched = self.skills.match("线上误杀告警，需要排查规则")
        self.assertTrue(matched)
        self.assertIn("rule-review-sop", {m.name for m in matched})

    def test_resource_escape_rejected(self) -> None:
        with self.assertRaises(PermissionError):
            self.skills.load_resource("rule-review-sop", "../SKILL.md")


class MemoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="memory-test-"))
        self.cfg = make_config(self.root)
        self.memory = MemoryManager(self.cfg)

    async def test_remember_and_search(self) -> None:
        record = MemoryRecord(
            kind="experience",
            text="误杀问题排查：先核对规则阈值，再检查特征分布漂移",
            tags=["误杀", "规则"],
            user_id=1,
        )
        stored = await self.memory.remember([record])
        self.assertEqual(stored, 1)
        results = await self.memory.recall("误杀 规则 阈值", user_id=1, top_k=3)
        self.assertTrue(results)
        self.assertIn("误杀", results[0].text)
        recent = await self.memory.recent(limit=5)
        self.assertTrue(any(item.id == record.id for item in recent))

    async def test_distill_run(self) -> None:
        task = TaskItem(
            seq=1,
            title="异常定位",
            agent="anomaly_diagnosis",
            status="completed",
            result_summary="定位到规则阈值调整导致误杀",
        )
        plan = TaskPlan(objective="分析误杀", tasks=[task])
        snapshot = RunSnapshot(id="run_distill", objective="分析误杀", plan=plan)
        result = SubAgentResult(
            task_id=task.id,
            agent=task.agent,
            success=True,
            summary="规则阈值从 0.9 下调到 0.82 导致误杀增加",
            findings=["误杀量上升 12%"],
        )
        records = await distill_run(snapshot, [result])
        self.assertTrue(records)
        self.assertTrue(any("排查模式" in record.text or "SOP" in record.text for record in records))
        stored = await self.memory.remember(records)
        self.assertGreaterEqual(stored, 1)


if __name__ == "__main__":
    unittest.main()
