"""虚拟文件系统（VFS）—— Agent 可持续操作的持久工作资产。

目录约定（逻辑路径 → 物理路径 ``data/harness/runs/{run_id}/fs``）::

    /workspace   策略 DSL、脚本、临时分析产物
    /reports     测试报告、效果对比、审批材料
    /logs        日志快照、工具输出
    /policies    规则配置与灰度批次
    /skills      本次运行使用到的技能快照
    /memories    经验沉淀草稿
    /sandbox     从沙箱平面晋升出来的产物

双层访问平面：
- ``host``    主 Agent 与子 Agent 直接读写的虚拟文件系统（权威资产）；
- ``sandbox`` 隔离沙箱工作区，代码/脚本执行只在此平面发生，
  产物需显式 ``promote()`` 才能进入 host 平面（默认触发人工审批）。

每次写入都会在 ``.versions/{path_hash}/vNNNN.json`` 留痕，支持历史查看与 diff。
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

from harness.config import HarnessConfig, get_harness_config
from harness.contracts import (
    ALLOWED_VFS_ROOTS,
    FileEntry,
    FilePlane,
    FileVersion,
    SearchHit,
    utcnow,
)
from server.utils.logger import logger

TEXT_SUFFIXES = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sql", ".log",
    ".csv", ".dsl", ".rule", ".conf", ".ini", ".toml", ".java", ".sh",
    ".ps1", ".js", ".ts", ".html", ".css",
}


class VFSError(Exception):
    """VFS 通用错误。"""


class VFSPermissionError(VFSError):
    """路径越界或目录不允许。"""


class VFSNotFound(VFSError):
    """文件不存在。"""


class VFSTooLarge(VFSError):
    """文件超过大小限制。"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plane_value(plane: FilePlane | str) -> str:
    return plane.value if isinstance(plane, FilePlane) else str(plane)


class VirtualFileSystem:
    """单次运行隔离的虚拟文件系统。"""

    _locks: dict[str, asyncio.Lock] = {}

    def __init__(
        self,
        run_id: str,
        config: HarnessConfig | None = None,
        base_dir: Path | None = None,
    ) -> None:
        self.run_id = run_id
        self.config = config or get_harness_config()
        self.run_dir = Path(base_dir) if base_dir else self.config.workspace_root / "runs" / run_id
        self.host_root = self.run_dir / "fs"
        self.sandbox_root = self.run_dir / "sandbox"
        self.versions_root = self.run_dir / ".versions"
        self.max_bytes = self.config.vfs_max_file_bytes
        self._lock = self._locks.setdefault(str(self.run_dir.resolve()), asyncio.Lock())
        self._ensure_layout()

    # ------------------------------------------------------------------ setup
    def _ensure_layout(self) -> None:
        for root in ALLOWED_VFS_ROOTS:
            (self.host_root / root.lstrip("/")).mkdir(parents=True, exist_ok=True)
            (self.sandbox_root / root.lstrip("/")).mkdir(parents=True, exist_ok=True)
        self.versions_root.mkdir(parents=True, exist_ok=True)

    def plane_root(self, plane: FilePlane | str = FilePlane.HOST) -> Path:
        return self.sandbox_root if _plane_value(plane) == FilePlane.SANDBOX.value else self.host_root

    # ------------------------------------------------------------------ paths
    def _resolve(
        self,
        path: str,
        plane: FilePlane | str = FilePlane.HOST,
        *,
        allow_root: bool = False,
    ) -> tuple[Path, str]:
        raw = (path or "").strip().replace("\\", "/")
        if not raw:
            raise VFSPermissionError("empty path")
        if not raw.startswith("/"):
            raw = "/" + raw
        pure = PurePosixPath(raw)
        parts = [p for p in pure.parts if p not in ("/", "", ".")]
        if any(p == ".." for p in parts):
            raise VFSPermissionError(f"path traversal rejected: {path}")
        if not parts:
            if allow_root:
                return self.plane_root(plane).resolve(), "/"
            raise VFSPermissionError("root path is not a file")
        top = "/" + parts[0]
        if top not in ALLOWED_VFS_ROOTS:
            raise VFSPermissionError(
                f"path outside allowed roots {ALLOWED_VFS_ROOTS}: {path}"
            )
        root = self.plane_root(plane)
        full = (root.joinpath(*parts)).resolve()
        if not full.is_relative_to(root.resolve()):
            raise VFSPermissionError(f"path escapes plane root: {path}")
        return full, "/" + "/".join(parts)

    def _version_dir(self, logical_path: str) -> Path:
        digest = hashlib.sha256(logical_path.encode("utf-8")).hexdigest()[:16]
        return self.versions_root / digest

    def _next_version(self, logical_path: str) -> int:
        vdir = self._version_dir(logical_path)
        if not vdir.exists():
            return 1
        numbers = []
        for item in vdir.glob("v*.json"):
            try:
                numbers.append(int(item.stem.lstrip("v")))
            except ValueError:
                continue
        return (max(numbers) + 1) if numbers else 1

    def _write_version(
        self,
        logical_path: str,
        content: str,
        *,
        author: str,
        comment: str,
        plane: FilePlane,
        deleted: bool = False,
    ) -> FileVersion:
        version = self._next_version(logical_path)
        record = {
            "path": logical_path,
            "version": version,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "size": len(content.encode("utf-8")),
            "author": author,
            "comment": comment,
            "plane": str(plane),
            "deleted": deleted,
            "created_at": _utcnow_iso(),
            "content": "" if deleted else content,
        }
        vdir = self._version_dir(logical_path)
        vdir.mkdir(parents=True, exist_ok=True)
        target = vdir / f"v{version:04d}.json"
        target.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return FileVersion(
            path=logical_path,
            version=version,
            sha256=record["sha256"],
            size=record["size"],
            author=author,
            comment=comment,
            created_at=datetime.fromisoformat(record["created_at"]),
        )

    def _latest_version(self, logical_path: str) -> tuple[int, str]:
        vdir = self._version_dir(logical_path)
        if not vdir.exists():
            return 0, ""
        files = sorted(vdir.glob("v*.json"))
        if not files:
            return 0, ""
        try:
            record = json.loads(files[-1].read_text(encoding="utf-8"))
            return int(record.get("version", 0)), record.get("author", "")
        except (OSError, json.JSONDecodeError, ValueError):
            return 0, ""

    # ------------------------------------------------------------------ write
    async def write(
        self,
        path: str,
        content: str,
        *,
        author: str = "agent",
        comment: str = "",
        plane: FilePlane | str = FilePlane.HOST,
    ) -> FileEntry:
        if not isinstance(content, str):
            content = str(content)
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise VFSTooLarge(
                f"{path} exceeds {self.max_bytes} bytes ({len(encoded)} bytes)"
            )
        async with self._lock:
            full, logical = self._resolve(path, plane)
            await asyncio.to_thread(self._atomic_write, full, content)
            version = await asyncio.to_thread(
                self._write_version,
                logical,
                content,
                author=author,
                comment=comment,
                plane=FilePlane(_plane_value(plane)),
            )
            return self._entry_for(full, logical, FilePlane(_plane_value(plane)), version.version, author)

    def _atomic_write(self, full: Path, content: str) -> None:
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.with_suffix(full.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(full)

    async def append(
        self,
        path: str,
        text: str,
        *,
        author: str = "agent",
        comment: str = "append",
        plane: FilePlane | str = FilePlane.HOST,
    ) -> FileEntry:
        async with self._lock:
            try:
                existing = await self.read(path, plane=plane)
            except VFSNotFound:
                existing = ""
            return await self.write(
                path, existing + text, author=author, comment=comment, plane=plane
            )

    async def edit(
        self,
        path: str,
        old: str,
        new: str,
        *,
        replace_all: bool = False,
        author: str = "agent",
        comment: str = "",
        plane: FilePlane | str = FilePlane.HOST,
    ) -> FileEntry:
        if not old:
            raise VFSError("old string must not be empty")
        content = await self.read(path, plane=plane)
        if old not in content:
            raise VFSNotFound(f"target text not found in {path}")
        updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        return await self.write(
            path,
            updated,
            author=author,
            comment=comment or "edit",
            plane=plane,
        )

    async def delete(
        self,
        path: str,
        *,
        author: str = "agent",
        plane: FilePlane | str = FilePlane.HOST,
    ) -> bool:
        async with self._lock:
            full, logical = self._resolve(path, plane)
            if not full.exists():
                raise VFSNotFound(path)
            if full.is_dir():
                await asyncio.to_thread(shutil.rmtree, full)
            else:
                await asyncio.to_thread(full.unlink)
            await asyncio.to_thread(
                self._write_version,
                logical,
                "",
                author=author,
                comment="delete",
                plane=FilePlane(_plane_value(plane)),
                deleted=True,
            )
            return True

    async def promote(
        self,
        sandbox_path: str,
        host_path: str,
        *,
        author: str = "sandbox",
        comment: str = "promote from sandbox",
    ) -> FileEntry:
        """把沙箱平面产物晋升到 host 平面（调用方负责审批）。"""
        content = await self.read(sandbox_path, plane=FilePlane.SANDBOX)
        return await self.write(host_path, content, author=author, comment=comment)

    # ------------------------------------------------------------------- read
    async def read(
        self,
        path: str,
        *,
        plane: FilePlane | str = FilePlane.HOST,
    ) -> str:
        full, _ = self._resolve(path, plane)
        return await asyncio.to_thread(self._read_sync, full, path)

    def _read_sync(self, full: Path, logical: str) -> str:
        if not full.exists() or full.is_dir():
            raise VFSNotFound(logical)
        if full.stat().st_size > self.max_bytes:
            raise VFSTooLarge(f"{logical} exceeds {self.max_bytes} bytes")
        try:
            return full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return full.read_text(encoding="utf-8", errors="replace")

    async def exists(self, path: str, *, plane: FilePlane | str = FilePlane.HOST) -> bool:
        try:
            full, _ = self._resolve(path, plane)
        except VFSError:
            return False
        return await asyncio.to_thread(full.exists)

    async def stat(
        self,
        path: str,
        *,
        plane: FilePlane | str = FilePlane.HOST,
    ) -> FileEntry:
        full, logical = self._resolve(path, plane)
        if not full.exists():
            raise VFSNotFound(path)
        version, author = await asyncio.to_thread(self._latest_version, logical)
        return await asyncio.to_thread(
            self._entry_for, full, logical, FilePlane(_plane_value(plane)), version, author
        )

    def _entry_for(
        self,
        full: Path,
        logical: str,
        plane: FilePlane,
        version: int,
        author: str,
    ) -> FileEntry:
        if full.is_dir():
            return FileEntry(
                path=logical,
                kind="dir",
                version=version,
                author=author,
                plane=plane,
            )
        content = ""
        try:
            with full.open("r", encoding="utf-8", errors="replace") as fh:
                content = fh.read(400)
        except OSError:
            pass
        digest = ""
        try:
            digest = hashlib.sha256(full.read_bytes()).hexdigest()
        except OSError:
            pass
        return FileEntry(
            path=logical,
            kind="file",
            size=full.stat().st_size,
            sha256=digest,
            version=version,
            author=author,
            updated_at=datetime.fromtimestamp(full.stat().st_mtime, timezone.utc),
            plane=plane,
            preview=content[:200],
        )

    async def list_dir(
        self,
        path: str = "/",
        *,
        plane: FilePlane | str = FilePlane.HOST,
        recursive: bool = False,
    ) -> list[FileEntry]:
        full, logical = self._resolve(path, plane, allow_root=True)
        if not full.exists():
            raise VFSNotFound(path)
        if full.is_file():
            return [await self.stat(path, plane=plane)]
        return await asyncio.to_thread(
            self._list_sync, full, logical, FilePlane(_plane_value(plane)), recursive
        )

    def _list_sync(
        self,
        full: Path,
        logical: str,
        plane: FilePlane,
        recursive: bool,
    ) -> list[FileEntry]:
        entries: list[FileEntry] = []
        iterator = full.rglob("*") if recursive else full.iterdir()
        for item in iterator:
            if any(part.startswith(".") for part in item.relative_to(full).parts):
                continue
            rel = item.relative_to(full)
            entry_path = f"{logical.rstrip('/')}/{rel.as_posix()}"
            version, author = self._latest_version(entry_path)
            entries.append(self._entry_for(item, entry_path, plane, version, author))
        entries.sort(key=lambda e: (e.kind != "dir", e.path))
        return entries

    async def tree(
        self,
        path: str = "/",
        *,
        depth: int = 3,
        plane: FilePlane | str = FilePlane.HOST,
    ) -> list[dict]:
        """轻量目录树，供前端文件浏览器使用。"""
        entries = await self.list_dir(path, plane=plane, recursive=True)
        root_depth = path.rstrip("/").count("/")
        result = []
        for entry in entries:
            if entry.path.count("/") - root_depth > depth:
                continue
            result.append(
                {
                    "path": entry.path,
                    "kind": entry.kind,
                    "size": entry.size,
                    "version": entry.version,
                    "updated_at": entry.updated_at.isoformat(),
                }
            )
        return result

    # ----------------------------------------------------------------- search
    async def search(
        self,
        query: str,
        *,
        path: str = "/",
        regex: bool = False,
        limit: int = 50,
        case_sensitive: bool = False,
        plane: FilePlane | str = FilePlane.HOST,
    ) -> list[SearchHit]:
        if not query:
            return []
        full, _ = self._resolve(path, plane, allow_root=True)
        if not full.exists():
            raise VFSNotFound(path)
        return await asyncio.to_thread(
            self._search_sync, full, query, regex, limit, case_sensitive, plane
        )

    def _search_sync(
        self,
        full: Path,
        query: str,
        regex: bool,
        limit: int,
        case_sensitive: bool,
        plane: FilePlane | str,
    ) -> list[SearchHit]:
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(query if regex else re.escape(query), flags)
        hits: list[SearchHit] = []
        files = [full] if full.is_file() else list(full.rglob("*"))
        for item in files:
            if not item.is_file() or item.stat().st_size > self.max_bytes:
                continue
            if item.suffix.lower() not in TEXT_SUFFIXES and item.suffix != "":
                continue
            try:
                text = item.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            logical = "/" + item.relative_to(self.plane_root(plane)).as_posix()
            for line_no, line in enumerate(text.splitlines(), start=1):
                match = pattern.search(line)
                if match:
                    hits.append(
                        SearchHit(
                            path=logical,
                            line_no=line_no,
                            line=line.strip()[:300],
                            match=match.group(0)[:100],
                        )
                    )
                    if len(hits) >= limit:
                        return hits
        return hits

    # ---------------------------------------------------------------- history
    async def history(
        self,
        path: str,
        *,
        plane: FilePlane | str = FilePlane.HOST,
    ) -> list[FileVersion]:
        _, logical = self._resolve(path, plane)
        return await asyncio.to_thread(self._history_sync, logical)

    def _history_sync(self, logical: str) -> list[FileVersion]:
        vdir = self._version_dir(logical)
        if not vdir.exists():
            return []
        versions: list[FileVersion] = []
        for item in sorted(vdir.glob("v*.json")):
            try:
                record = json.loads(item.read_text(encoding="utf-8"))
                versions.append(
                    FileVersion(
                        path=record["path"],
                        version=record["version"],
                        sha256=record.get("sha256", ""),
                        size=record.get("size", 0),
                        author=record.get("author", ""),
                        comment=record.get("comment", ""),
                        created_at=datetime.fromisoformat(record["created_at"]),
                    )
                )
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
        return versions

    async def read_version(self, path: str, version: int) -> str:
        _, logical = self._resolve(path)
        return await asyncio.to_thread(self._read_version_sync, logical, version)

    def _read_version_sync(self, logical: str, version: int) -> str:
        target = self._version_dir(logical) / f"v{version:04d}.json"
        if not target.exists():
            raise VFSNotFound(f"{logical}@v{version}")
        record = json.loads(target.read_text(encoding="utf-8"))
        if record.get("deleted"):
            return ""
        return record.get("content", "")

    async def diff(self, path: str, from_version: int, to_version: int) -> str:
        old = await self.read_version(path, from_version)
        new = await self.read_version(path, to_version)
        diff_lines = difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"{path}@v{from_version}",
            tofile=f"{path}@v{to_version}",
            lineterm="",
        )
        return "\n".join(diff_lines)

    # ---------------------------------------------------------------- helpers
    async def seed_manifest(self, payload: dict) -> FileEntry:
        """写入运行清单，方便人工排查与前端展示。"""
        return await self.write(
            "/workspace/run_manifest.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            author="harness",
            comment="run manifest",
        )

    def logical_files(self, prefix: str = "/") -> Iterable[str]:
        """同步枚举逻辑路径（仅供内部诊断）。"""
        full, logical = self._resolve(prefix)
        for item in full.rglob("*"):
            if item.is_file():
                yield f"{logical.rstrip('/')}/{item.relative_to(full).as_posix()}"
