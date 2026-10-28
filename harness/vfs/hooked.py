"""带中间件的虚拟文件系统代理。

Agent 的所有文件读写都经过这里，从而真正触发 ``BEFORE_FILE_READ`` /
``BEFORE_FILE_WRITE`` / ``AFTER_FILE_*`` Hook：
- 权限中间件可拒绝 viewer 的写操作；
- PII 中间件可在落盘前脱敏；
- 追踪与审计中间件记录文件操作。

运行时的内部操作（写 run_manifest、最终报告）仍可使用原始 VFS，避免自审批。
"""
from __future__ import annotations

from typing import Any

from harness.contracts import FilePlane, HookPoint
from harness.middleware.base import MiddlewareChain, build_context
from harness.vfs.store import VirtualFileSystem


def _plane_value(plane: FilePlane | str) -> str:
    return plane.value if isinstance(plane, FilePlane) else str(plane)


class HookedFileSystem:
    def __init__(
        self,
        inner: VirtualFileSystem,
        chain: MiddlewareChain,
        services: dict[str, Any],
        *,
        run_id: str,
        user_id: int | None = None,
        agent: str = "agent",
    ) -> None:
        self.inner = inner
        self.chain = chain
        self.services = services
        self.run_id = run_id
        self.user_id = user_id
        self.agent = agent

    # ------------------------------------------------------------------ write
    async def write(
        self,
        path: str,
        content: str,
        *,
        author: str = "agent",
        comment: str = "",
        plane: FilePlane | str = FilePlane.HOST,
    ):
        ctx = build_context(
            self.run_id,
            HookPoint.BEFORE_FILE_WRITE,
            payload={
                "path": path,
                "content": content,
                "author": author,
                "comment": comment,
                "plane": _plane_value(plane),
            },
            user_id=self.user_id,
            agent=self.agent,
            services=self.services,
        )

        async def operation():
            return await self.inner.write(
                path,
                ctx.payload.get("content", content),
                author=author,
                comment=comment,
                plane=plane,
            )

        return await self.chain.run(ctx, operation)

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
    ):
        content = await self.read(path, plane=plane)
        if old not in content:
            from harness.vfs.store import VFSNotFound

            raise VFSNotFound(f"target text not found in {path}")
        updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        return await self.write(
            path, updated, author=author, comment=comment or "edit", plane=plane
        )

    async def append(
        self,
        path: str,
        text: str,
        *,
        author: str = "agent",
        comment: str = "append",
        plane: FilePlane | str = FilePlane.HOST,
    ):
        try:
            existing = await self.read(path, plane=plane)
        except Exception:
            existing = ""
        return await self.write(
            path, existing + text, author=author, comment=comment, plane=plane
        )

    async def delete(
        self,
        path: str,
        *,
        author: str = "agent",
        plane: FilePlane | str = FilePlane.HOST,
    ) -> bool:
        ctx = build_context(
            self.run_id,
            HookPoint.BEFORE_FILE_WRITE,
            payload={
                "path": path,
                "content": "",
                "delete": True,
                "author": author,
                "plane": _plane_value(plane),
            },
            user_id=self.user_id,
            agent=self.agent,
            services=self.services,
        )

        async def operation():
            return await self.inner.delete(path, author=author, plane=plane)

        return await self.chain.run(ctx, operation)

    async def promote(
        self,
        sandbox_path: str,
        host_path: str,
        *,
        author: str = "sandbox",
        comment: str = "promote from sandbox",
    ):
        content = await self.read(sandbox_path, plane=FilePlane.SANDBOX)
        return await self.write(host_path, content, author=author, comment=comment)

    # ------------------------------------------------------------------- read
    async def read(self, path: str, *, plane: FilePlane | str = FilePlane.HOST) -> str:
        ctx = build_context(
            self.run_id,
            HookPoint.BEFORE_FILE_READ,
            payload={"path": path, "plane": _plane_value(plane)},
            user_id=self.user_id,
            agent=self.agent,
            services=self.services,
        )

        async def operation():
            return await self.inner.read(path, plane=plane)

        return await self.chain.run(ctx, operation)

    # -------------------------------------------------------------- delegation
    async def exists(self, path: str, *, plane: FilePlane | str = FilePlane.HOST) -> bool:
        return await self.inner.exists(path, plane=plane)

    async def stat(self, path: str, *, plane: FilePlane | str = FilePlane.HOST):
        return await self.inner.stat(path, plane=plane)

    async def list_dir(self, path: str = "/", *, plane=FilePlane.HOST, recursive: bool = False):
        return await self.inner.list_dir(path, plane=plane, recursive=recursive)

    async def tree(self, path: str = "/", *, depth: int = 3, plane=FilePlane.HOST):
        return await self.inner.tree(path, depth=depth, plane=plane)

    async def search(self, query: str, **kwargs):
        return await self.inner.search(query, **kwargs)

    async def history(self, path: str, *, plane=FilePlane.HOST):
        return await self.inner.history(path, plane=plane)

    async def read_version(self, path: str, version: int) -> str:
        return await self.inner.read_version(path, version)

    async def diff(self, path: str, from_version: int, to_version: int) -> str:
        return await self.inner.diff(path, from_version, to_version)

    async def seed_manifest(self, payload: dict):
        return await self.inner.seed_manifest(payload)

    def plane_root(self, plane=FilePlane.HOST):
        return self.inner.plane_root(plane)
