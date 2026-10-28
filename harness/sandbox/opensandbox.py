"""OpenSandbox 适配器 —— 生产环境的主机+沙箱双层隔离执行平面。

对接约定（可通过配置调整 base_url）::

    POST   /api/v1/sandboxes                    创建沙箱会话
    POST   /api/v1/sandboxes/{id}/exec          执行命令
    PUT    /api/v1/sandboxes/{id}/files?path=   上传文件
    GET    /api/v1/sandboxes/{id}/files?path=   下载文件
    DELETE /api/v1/sandboxes/{id}               销毁会话

任何网络/协议失败都会抛出 :class:`SandboxUnavailable`，由 :class:`SandboxManager`
切换到本地受限沙箱，保证运行不中断。
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from harness.config import SandboxConfig, get_harness_config
from harness.contracts import SandboxExecResult, new_id
from harness.sandbox.base import BaseSandbox, SandboxSession
from harness.sandbox.policy import SandboxPolicy
from server.utils.logger import logger


class SandboxUnavailable(RuntimeError):
    pass


class OpenSandbox(BaseSandbox):
    provider_name = "opensandbox"

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self.config = config or get_harness_config().sandbox
        self.policy = SandboxPolicy(self.config)
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                headers=headers,
                timeout=self.config.command_timeout + 10,
            )
        return self._client

    async def start(self, run_id: str, workspace: Path) -> SandboxSession:
        workspace.mkdir(parents=True, exist_ok=True)
        payload = {
            "image": self.config.image,
            "namespace": self.config.namespace,
            "ttl": self.config.workspace_ttl,
            "network": "bridge" if self.config.allow_network else "none",
            "metadata": {"run_id": run_id, "managed_by": "agent-harness"},
        }
        try:
            response = await self.client.post("/api/v1/sandboxes", json=payload)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise SandboxUnavailable(f"OpenSandbox create failed: {exc}") from exc

        body = data.get("data", data) if isinstance(data, dict) else {}
        sandbox_id = (
            body.get("id")
            or body.get("sandbox_id")
            or body.get("sandboxId")
            or new_id("sbx_")
        )
        logger.info(f"[Sandbox] OpenSandbox session started: {sandbox_id}")
        return SandboxSession(
            id=str(sandbox_id),
            provider=self.provider_name,
            run_id=run_id,
            workspace=workspace,
            remote=True,
            metadata={"raw": body},
        )

    async def exec(
        self,
        session: SandboxSession,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxExecResult:
        started = time.perf_counter()
        decision = self.policy.check(command)
        if not decision.allowed:
            return SandboxExecResult(
                success=False,
                exit_code=-1,
                stderr=decision.reason,
                error=decision.reason,
                sandbox_id=session.id,
                provider=self.provider_name,
            )
        payload = {
            "command": command,
            "cwd": cwd or "workspace",
            "timeout": timeout or self.config.command_timeout,
            "env": env or {},
        }
        try:
            response = await self.client.post(
                f"/api/v1/sandboxes/{session.id}/exec", json=payload
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise SandboxUnavailable(f"OpenSandbox exec failed: {exc}") from exc

        body = data.get("data", data) if isinstance(data, dict) else {}
        stdout = str(body.get("stdout", ""))
        stderr = str(body.get("stderr", ""))
        truncated = False
        if len(stdout.encode("utf-8")) > self.config.max_output_bytes:
            stdout = stdout.encode("utf-8")[: self.config.max_output_bytes].decode(
                "utf-8", errors="replace"
            )
            truncated = True
        if len(stderr.encode("utf-8")) > self.config.max_output_bytes:
            stderr = stderr.encode("utf-8")[: self.config.max_output_bytes].decode(
                "utf-8", errors="replace"
            )
            truncated = True
        exit_code = int(body.get("exit_code", body.get("exitCode", 0)) or 0)
        return SandboxExecResult(
            success=bool(body.get("success", exit_code == 0)),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.perf_counter() - started) * 1000),
            truncated=truncated,
            sandbox_id=session.id,
            provider=self.provider_name,
        )

    async def upload(self, session: SandboxSession, path: str, content: str) -> None:
        try:
            response = await self.client.put(
                f"/api/v1/sandboxes/{session.id}/files",
                params={"path": path},
                content=content.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
            response.raise_for_status()
        except Exception as exc:
            raise SandboxUnavailable(f"OpenSandbox upload failed: {exc}") from exc

    async def download(self, session: SandboxSession, path: str) -> str:
        try:
            response = await self.client.get(
                f"/api/v1/sandboxes/{session.id}/files", params={"path": path}
            )
            response.raise_for_status()
            return response.text
        except Exception as exc:
            raise SandboxUnavailable(f"OpenSandbox download failed: {exc}") from exc

    async def stop(self, session: SandboxSession) -> None:
        try:
            await self.client.delete(f"/api/v1/sandboxes/{session.id}")
        except Exception as exc:
            logger.warning(f"[Sandbox] OpenSandbox stop failed: {exc}")

    def status(self, session: SandboxSession) -> dict:
        return {
            "id": session.id,
            "provider": self.provider_name,
            "remote": True,
            "workspace": str(session.workspace),
            "base_url": self.config.base_url,
            "policy": self.policy.describe(),
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
