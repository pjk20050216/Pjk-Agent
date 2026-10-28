"""中间件：缓存、权限、PII、敏感动作审批（通过/拒绝）。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path

from harness.contracts import HookPoint, RiskLevel
from harness.middleware.base import HookDenied, build_context
from harness.middleware.builtin import build_default_chain
from harness.tools.base import HarnessTool, schema
from harness.tools.context import ToolContext
from harness.vfs.store import VirtualFileSystem
from tests.base import make_harness, noop_status


class MiddlewareTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mw-test-"))
        self.harness, self.store, self.cfg = make_harness(self.root)
        self.vfs = VirtualFileSystem("run_mw", self.cfg)
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

    async def test_cache_short_circuits_second_call(self) -> None:
        calls = {"n": 0}

        async def handler(**kwargs):
            calls["n"] += 1
            return {"value": calls["n"]}

        tool = HarnessTool(
            name=f"cache_probe_{uuid.uuid4().hex[:8]}",
            description="probe",
            parameters=schema({}),
            handler=handler,
            cacheable=True,
        )

        def context():
            return build_context(
                "run_cache",
                HookPoint.BEFORE_TOOL,
                payload={
                    "tool_name": tool.name,
                    "arguments": {},
                    "tool_spec": tool.spec(),
                    "cacheable": True,
                    "max_attempts": 1,
                },
                services=self.services,
            )

        first = await self.chain.run(context(), lambda: tool.run())
        second = await self.chain.run(context(), lambda: tool.run())
        self.assertEqual(first, {"value": 1})
        self.assertEqual(second, {"value": 1})
        self.assertEqual(calls["n"], 1)

    async def test_permission_denies_viewer(self) -> None:
        tool = HarnessTool(
            name="publish_probe",
            description="probe",
            parameters=schema({}),
            handler=lambda **kwargs: asyncio.sleep(0, result={}),
            risk=RiskLevel.HIGH,
            action="rule.publish",
            requires_approval=True,
        )
        services = dict(self.services)
        services["user_role"] = "viewer"
        chain = build_default_chain(services)
        ctx = build_context(
            "run_perm",
            HookPoint.BEFORE_TOOL,
            payload={
                "tool_name": tool.name,
                "arguments": {},
                "tool_spec": tool.spec(),
                "cacheable": False,
            },
            services=services,
        )
        with self.assertRaises(HookDenied) as captured:
            await chain.run(ctx, lambda: tool.run())
        self.assertEqual(captured.exception.code, "permission_denied")

    async def test_pii_masked_before_model(self) -> None:
        ctx = build_context(
            "run_pii",
            HookPoint.BEFORE_MODEL,
            payload={
                "messages": [
                    {"role": "user", "content": "手机号 13800138000，邮箱 a@b.com"}
                ],
                "cacheable": False,
            },
            services=self.services,
        )

        async def operation():
            return "ok"

        await self.chain.run(ctx, operation)
        content = ctx.payload["messages"][0]["content"]
        self.assertNotIn("13800138000", content)
        self.assertNotIn("a@b.com", content)
        self.assertIn("pii_findings", ctx.metadata)

    async def test_file_hooks_permission_and_pii(self) -> None:
        from harness.vfs.hooked import HookedFileSystem

        viewer_services = dict(self.services)
        viewer_services["user_role"] = "viewer"
        viewer_fs = HookedFileSystem(
            self.vfs,
            self.chain,
            viewer_services,
            run_id="run_file_viewer",
            user_id=1,
        )
        with self.assertRaises(HookDenied):
            await viewer_fs.write("/workspace/blocked.md", "x")

        operator_fs = HookedFileSystem(
            self.vfs,
            self.chain,
            self.services,
            run_id="run_file_operator",
            user_id=1,
        )
        await operator_fs.write("/workspace/pii.md", "手机号 13800138000")
        stored = await self.vfs.read("/workspace/pii.md")
        self.assertNotIn("13800138000", stored)
        self.assertIn("*", stored)

    async def _sandbox_tool_and_context(self, run_id: str):
        ctx_tool = ToolContext(
            run_id=run_id,
            user_id=1,
            vfs=self.vfs,
            memory=self.harness.memory,
            sandbox=self.harness.sandbox,
            skills=self.harness.skills,
            approvals=self.harness.approvals,
        )
        from harness.tools import SUBAGENT_TOOLSETS, build_registry

        registry = await build_registry(ctx_tool, SUBAGENT_TOOLSETS["test_validation"])
        tool = registry.get("sandbox_exec")
        ctx = build_context(
            run_id,
            HookPoint.BEFORE_TOOL,
            payload={
                "tool_name": "sandbox_exec",
                "arguments": {"command": "echo approved"},
                "tool_spec": tool.spec(),
                "target": "echo",
                "cacheable": False,
            },
            user_id=1,
            agent="test_validation",
            services=self.services,
        )
        return tool, ctx

    async def test_sensitive_action_approved(self) -> None:
        run = await self.harness.create_run("审批通过测试", user_id=1)
        tool, ctx = await self._sandbox_tool_and_context(run.id)

        async def approve():
            for _ in range(50):
                await asyncio.sleep(0.05)
                pending = self.harness.approvals.pending(run.id)
                if pending:
                    await self.harness.approvals.decide(
                        pending[0].id, approved=True, decided_by="tester"
                    )
                    return
            self.fail("no pending approval")

        task = asyncio.create_task(approve())
        result = await self.chain.run(ctx, lambda: tool.run(command="echo approved"))
        await task
        self.assertTrue(result["success"])
        self.assertIn("approved", result["stdout"])
        self.assertIsNotNone(ctx.payload.get("approval_id"))

    async def test_sensitive_action_rejected(self) -> None:
        run = await self.harness.create_run("审批拒绝测试", user_id=1)
        tool, ctx = await self._sandbox_tool_and_context(run.id)

        async def reject():
            for _ in range(50):
                await asyncio.sleep(0.05)
                pending = self.harness.approvals.pending(run.id)
                if pending:
                    await self.harness.approvals.decide(
                        pending[0].id,
                        approved=False,
                        decided_by="tester",
                        comment="risk too high",
                    )
                    return
            self.fail("no pending approval")

        task = asyncio.create_task(reject())
        with self.assertRaises(HookDenied) as captured:
            await self.chain.run(ctx, lambda: tool.run(command="echo rejected"))
        await task
        self.assertEqual(captured.exception.code, "approval_rejected")


if __name__ == "__main__":
    unittest.main()
