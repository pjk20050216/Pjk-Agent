"""HTTP API 冒烟：鉴权覆盖、状态、技能、工具、创建运行、文件与追踪。"""
from __future__ import annotations

import time
import unittest

from fastapi.testclient import TestClient

from server.dependencies import get_current_user
from server.main import app


class FakeUser:
    id = 1
    username = "tester"
    email = "tester@example.com"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app.dependency_overrides[get_current_user] = lambda: FakeUser()
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        app.dependency_overrides.clear()

    def test_status_and_catalog(self) -> None:
        status = self.client.get("/api/harness/status")
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertIn("harness", body)
        self.assertGreaterEqual(body["harness"]["skills"], 5)

        skills = self.client.get("/api/harness/skills").json()
        self.assertGreaterEqual(skills["count"], 5)

        agents = self.client.get("/api/harness/agents").json()
        self.assertEqual(len(agents["agents"]), 6)
        self.assertIn(
            "data_collection", {item["name"] for item in agents["agents"]}
        )

        tools = self.client.get("/api/harness/tools").json()
        self.assertGreaterEqual(tools["count"], 20)

    def test_run_lifecycle(self) -> None:
        response = self.client.post(
            "/api/harness/runs",
            json={"objective": "分析 R_RISK_001 误杀问题并给出调整与回测建议"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        run = response.json()
        run_id = run["id"]

        detail = None
        for _ in range(80):
            detail = self.client.get(f"/api/harness/runs/{run_id}").json()
            if detail["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.25)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["status"], "completed", detail.get("error"))

        tasks = self.client.get(f"/api/harness/runs/{run_id}/tasks").json()
        self.assertEqual(len(tasks["tasks"]), 6)

        report_path = detail["result"]["report_path"]
        file_response = self.client.get(
            f"/api/harness/runs/{run_id}/file", params={"path": report_path}
        )
        self.assertEqual(file_response.status_code, 200)
        self.assertIn("结论摘要", file_response.json()["content"])

        history = self.client.get(
            f"/api/harness/runs/{run_id}/file/history", params={"path": report_path}
        ).json()
        self.assertGreaterEqual(len(history["versions"]), 1)

        traces = self.client.get(f"/api/harness/runs/{run_id}/traces").json()
        self.assertGreater(traces["count"], 0)

        replay = self.client.get(f"/api/harness/runs/{run_id}/events/replay").json()
        self.assertGreater(replay["total"], 0)

    def test_approval_list_requires_ownership(self) -> None:
        response = self.client.get("/api/harness/approvals")
        self.assertEqual(response.status_code, 200)
        self.assertIn("approvals", response.json())


if __name__ == "__main__":
    unittest.main()
