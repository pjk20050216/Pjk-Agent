"""本地受限子进程沙箱 —— OpenSandbox 不可用时的回退实现。

隔离手段（按强度排序）：
- 文件：cwd 固定为运行沙箱工作区，路径穿越拒绝；env 中 HOME/TMP 重定向到工作区；
- 进程：独立进程组，超时整组 kill，避免残留子进程；
- 网络：未显式允许时注入不可达代理，阻断 requests/httpx/pip 等常见出口（纵深防御，
  不是内核级网络隔离——生产请使用 OpenSandbox / 容器运行时）；
- 命令：白名单 + 高危正则拒绝（见 policy.py）；
- 资源：输出截断、超时上限。

注意：本实现用于开发/单机演示与 OpenSandbox 故障降级，生产环境应强制
``HARNESS_SANDBOX_PROVIDER=opensandbox``。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from harness.config import SandboxConfig, get_harness_config
from harness.contracts import SandboxExecResult, new_id
from harness.sandbox.base import BaseSandbox, SandboxSession
from harness.sandbox.policy import SandboxPolicy
from server.utils.logger import logger

_SAFE_ENV_KEYS = (
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR",
    "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONIOENCODING",
)


class LocalSandbox(BaseSandbox):
    provider_name = "local"

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self.config = config or get_harness_config().sandbox
        self.policy = SandboxPolicy(self.config)

    async def start(self, run_id: str, workspace: Path) -> SandboxSession:
        workspace.mkdir(parents=True, exist_ok=True)
        for sub in ("workspace", "reports", "logs", "tmp"):
            (workspace / sub).mkdir(parents=True, exist_ok=True)
        session = SandboxSession(
            id=new_id("sbx_"),
            provider=self.provider_name,
            run_id=run_id,
            workspace=workspace,
            remote=False,
        )
        logger.info(f"[Sandbox] local session started: {session.id} ({workspace})")
        return session

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
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        workdir = self._safe_cwd(session, cwd)
        proc_env = self._build_env(session, env)
        timeout_s = timeout or self.config.command_timeout

        try:
            if os.name == "nt":
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(workdir),
                    env=proc_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(workdir),
                    env=proc_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
        except Exception as exc:
            return SandboxExecResult(
                success=False,
                exit_code=-1,
                stderr=str(exc),
                error=f"failed to start process: {exc}",
                sandbox_id=session.id,
                provider=self.provider_name,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            await self._kill(proc)
            return SandboxExecResult(
                success=False,
                exit_code=-1,
                stderr=f"command timed out after {timeout_s}s",
                error=f"timeout after {timeout_s}s",
                sandbox_id=session.id,
                provider=self.provider_name,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        stdout, truncated_out = self._truncate(stdout_b.decode("utf-8", errors="replace"))
        stderr, truncated_err = self._truncate(stderr_b.decode("utf-8", errors="replace"))
        exit_code = proc.returncode or 0
        return SandboxExecResult(
            success=exit_code == 0,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.perf_counter() - started) * 1000),
            truncated=truncated_out or truncated_err,
            sandbox_id=session.id,
            provider=self.provider_name,
        )

    # ------------------------------------------------------------------ files
    async def upload(self, session: SandboxSession, path: str, content: str) -> None:
        target = self._safe_path(session, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def download(self, session: SandboxSession, path: str) -> str:
        target = self._safe_path(session, path)
        if not target.is_file():
            raise FileNotFoundError(path)
        return target.read_text(encoding="utf-8", errors="replace")

    async def stop(self, session: SandboxSession) -> None:
        logger.info(f"[Sandbox] local session stopped: {session.id}")

    def status(self, session: SandboxSession) -> dict:
        return {
            "id": session.id,
            "provider": self.provider_name,
            "remote": False,
            "workspace": str(session.workspace),
            "policy": self.policy.describe(),
        }

    # ----------------------------------------------------------------- helpers
    def _safe_cwd(self, session: SandboxSession, cwd: str | None) -> Path:
        base = session.workspace.resolve()
        target = (base / (cwd or "workspace")).resolve() if cwd else (base / "workspace")
        if not target.is_relative_to(base):
            raise PermissionError(f"cwd escapes sandbox workspace: {cwd}")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _safe_path(self, session: SandboxSession, path: str) -> Path:
        base = session.workspace.resolve()
        target = (base / path.lstrip("/\\")).resolve()
        if not target.is_relative_to(base):
            raise PermissionError(f"path escapes sandbox workspace: {path}")
        return target

    def _build_env(
        self, session: SandboxSession, extra: dict[str, str] | None
    ) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in _SAFE_ENV_KEYS:
            if key in os.environ:
                env[key] = os.environ[key]
        env.setdefault("PATH", os.environ.get("PATH", ""))
        env["HOME"] = str(session.workspace)
        env["USERPROFILE"] = str(session.workspace)
        env["TMPDIR"] = str(session.workspace / "tmp")
        env["TEMP"] = str(session.workspace / "tmp")
        env["TMP"] = str(session.workspace / "tmp")
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        if not self.config.allow_network:
            blocked = "http://127.0.0.1:9"
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env[key] = blocked
            env["NO_PROXY"] = ""
            env["no_proxy"] = ""

        if extra:
            for key, value in extra.items():
                if key.upper().startswith(("LD_", "DYLD_")) or key in {"PYTHONPATH"}:
                    continue
                env[key] = str(value)
        return env

    def _truncate(self, text: str) -> tuple[str, bool]:
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= self.config.max_output_bytes:
            return text, False
        return encoded[: self.config.max_output_bytes].decode("utf-8", errors="replace"), True

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "nt":
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
