"""沙箱管理器 —— 选择 provider、维护会话、自动降级。

策略：
- ``HARNESS_SANDBOX_PROVIDER=opensandbox`` 时优先远程；
- 远程创建/执行失败 → 记录告警、销毁会话、切换到本地受限沙箱并重试一次；
- 会话按 run_id 复用，运行结束由 runtime 调用 ``stop()`` 清理。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from harness.config import HarnessConfig, get_harness_config
from harness.contracts import SandboxExecResult
from harness.sandbox.base import BaseSandbox, SandboxSession
from harness.sandbox.local import LocalSandbox
from harness.sandbox.opensandbox import OpenSandbox, SandboxUnavailable
from harness.sandbox.policy import PolicyDecision, SandboxPolicy
from server.utils.logger import logger


class SandboxManager:
    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config or get_harness_config()
        self.local = LocalSandbox(self.config.sandbox)
        self.remote: OpenSandbox | None = (
            OpenSandbox(self.config.sandbox)
            if self.config.sandbox.enabled and self.config.sandbox.provider == "opensandbox"
            else None
        )
        self._sessions: dict[str, SandboxSession] = {}
        self._lock = asyncio.Lock()
        self._degraded = False

    @property
    def policy(self) -> SandboxPolicy:
        return self.local.policy

    def check(self, command: str) -> PolicyDecision:
        return self.policy.check(command)

    def workspace(self, run_id: str) -> Path:
        return self.config.workspace_root / "runs" / run_id / "sandbox"

    async def session(self, run_id: str) -> SandboxSession:
        async with self._lock:
            existing = self._sessions.get(run_id)
            if existing is not None:
                return existing
            workspace = self.workspace(run_id)
            session: SandboxSession | None = None
            if self.remote is not None and not self._degraded:
                try:
                    session = await self.remote.start(run_id, workspace)
                except Exception as exc:
                    self._degraded = True
                    logger.warning(
                        f"[Sandbox] OpenSandbox unavailable ({exc}); "
                        "falling back to local restricted sandbox"
                    )
            if session is None:
                session = await self.local.start(run_id, workspace)
            self._sessions[run_id] = session
            return session

    async def exec(
        self,
        run_id: str,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxExecResult:
        session = await self.session(run_id)
        if session.remote and self.remote is not None:
            try:
                return await self.remote.exec(
                    session, command, cwd=cwd, timeout=timeout, env=env
                )
            except SandboxUnavailable as exc:
                logger.warning(f"[Sandbox] remote exec failed ({exc}); retrying locally")
                await self.remote.stop(session)
                self._degraded = True
                async with self._lock:
                    self._sessions.pop(run_id, None)
                session = await self.session(run_id)
        return await self.local.exec(
            session, command, cwd=cwd, timeout=timeout, env=env
        )

    async def write_file(self, run_id: str, path: str, content: str) -> None:
        session = await self.session(run_id)
        if session.remote and self.remote is not None:
            try:
                await self.remote.upload(session, path, content)
                return
            except SandboxUnavailable as exc:
                logger.warning(f"[Sandbox] remote upload failed ({exc}); retrying locally")
                self._degraded = True
        await self.local.upload(session, path, content)

    async def read_file(self, run_id: str, path: str) -> str:
        session = await self.session(run_id)
        if session.remote and self.remote is not None:
            try:
                return await self.remote.download(session, path)
            except SandboxUnavailable as exc:
                logger.warning(f"[Sandbox] remote download failed ({exc}); retrying locally")
                self._degraded = True
        return await self.local.download(session, path)

    async def stop(self, run_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(run_id, None)
        if session is None:
            return
        try:
            if session.remote and self.remote is not None:
                await self.remote.stop(session)
            else:
                await self.local.stop(session)
        except Exception as exc:
            logger.warning(f"[Sandbox] stop failed: {exc}")

    async def close(self) -> None:
        for run_id in list(self._sessions):
            await self.stop(run_id)
        if self.remote is not None:
            await self.remote.aclose()

    def status(self, run_id: str | None = None) -> dict:
        if run_id:
            session = self._sessions.get(run_id)
            if session is None:
                return {"run_id": run_id, "active": False, "policy": self.policy.describe()}
            provider: BaseSandbox = self.remote if session.remote and self.remote else self.local
            return {"run_id": run_id, "active": True, **provider.status(session)}
        return {
            "provider": self.config.sandbox.provider,
            "enabled": self.config.sandbox.enabled,
            "degraded": self._degraded,
            "active_sessions": len(self._sessions),
            "policy": self.policy.describe(),
        }


sandbox_manager = SandboxManager()
