"""沙箱抽象 —— 统一的会话与执行接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.contracts import SandboxExecResult


@dataclass
class SandboxSession:
    id: str
    provider: str
    run_id: str
    workspace: Path
    remote: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseSandbox(ABC):
    provider_name = "base"

    @abstractmethod
    async def start(self, run_id: str, workspace: Path) -> SandboxSession:
        ...

    @abstractmethod
    async def exec(
        self,
        session: SandboxSession,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxExecResult:
        ...

    @abstractmethod
    async def upload(self, session: SandboxSession, path: str, content: str) -> None:
        ...

    @abstractmethod
    async def download(self, session: SandboxSession, path: str) -> str:
        ...

    @abstractmethod
    async def stop(self, session: SandboxSession) -> None:
        ...

    @abstractmethod
    def status(self, session: SandboxSession) -> dict:
        ...
