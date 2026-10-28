"""虚拟文件系统：读写、版本、diff、搜索、越界防护、双层平面。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.contracts import FilePlane
from harness.vfs.store import VFSPermissionError, VFSNotFound, VFSTooLarge, VirtualFileSystem
from tests.base import make_config


class VFSTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="vfs-test-"))
        self.cfg = make_config(self.root)
        self.vfs = VirtualFileSystem("run_test", self.cfg)

    async def test_write_read_edit_and_history(self) -> None:
        entry = await self.vfs.write("/workspace/demo.md", "line one\n", author="tester")
        self.assertEqual(entry.version, 1)
        self.assertEqual(await self.vfs.read("/workspace/demo.md"), "line one\n")

        await self.vfs.edit("/workspace/demo.md", "one", "two", author="tester")
        self.assertIn("line two", await self.vfs.read("/workspace/demo.md"))

        history = await self.vfs.history("/workspace/demo.md")
        self.assertEqual([item.version for item in history], [1, 2])
        diff = await self.vfs.diff("/workspace/demo.md", 1, 2)
        self.assertIn("+line two", diff)

    async def test_search_and_list(self) -> None:
        await self.vfs.write("/reports/a.md", "风险 规则 误杀", author="tester")
        await self.vfs.write("/logs/b.log", "decision=reject", author="tester")
        hits = await self.vfs.search("误杀")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].path, "/reports/a.md")

        entries = await self.vfs.list_dir("/")
        paths = {entry.path for entry in entries}
        self.assertIn("/workspace", paths)
        self.assertIn("/reports", paths)

    async def test_path_traversal_rejected(self) -> None:
        with self.assertRaises(VFSPermissionError):
            await self.vfs.read("/../etc/passwd")
        with self.assertRaises(VFSPermissionError):
            await self.vfs.write("/workspace/../../evil.md", "x")

    async def test_missing_file_and_size_limit(self) -> None:
        with self.assertRaises(VFSNotFound):
            await self.vfs.read("/workspace/nope.md")
        self.cfg.vfs_max_file_bytes = 10
        vfs = VirtualFileSystem("run_small", self.cfg)
        with self.assertRaises(VFSTooLarge):
            await vfs.write("/workspace/big.txt", "x" * 50)

    async def test_dual_plane_and_promote(self) -> None:
        await self.vfs.write("/workspace/script.py", "print('host')")
        await self.vfs.write(
            "/workspace/script.py", "print('sandbox')", plane=FilePlane.SANDBOX
        )
        self.assertIn("host", await self.vfs.read("/workspace/script.py"))
        self.assertIn(
            "sandbox",
            await self.vfs.read("/workspace/script.py", plane=FilePlane.SANDBOX),
        )
        entry = await self.vfs.promote(
            "/workspace/script.py", "/workspace/promoted.py"
        )
        self.assertEqual(entry.path, "/workspace/promoted.py")
        self.assertIn("sandbox", await self.vfs.read("/workspace/promoted.py"))


if __name__ == "__main__":
    unittest.main()
