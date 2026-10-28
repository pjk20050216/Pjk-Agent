"""Java 接口扩展桥 —— 把 Java 侧注册的工具接入 Python Harness。

设计：
- Java 服务暴露 ``GET {manifest_path}`` 返回工具清单（name/description/parameters/risk/action）；
- Python 通过 ``POST {manifest_path}/{name}/invoke`` 调用，请求体含 arguments/run_id/user_id；
- 双方共享 ``HARNESS_JAVA_SHARED_SECRET``，用 HMAC-SHA256 对 ``timestamp.body`` 签名，
  防止内网伪造调用；
- 桥不可用时静默降级（不注册 Java 工具），不影响内置能力。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from harness.config import JavaBridgeConfig, get_harness_config
from server.utils.logger import logger


class JavaToolBridge:
    def __init__(self, config: JavaBridgeConfig | None = None) -> None:
        self.config = config or get_harness_config().java_bridge
        self._client: httpx.AsyncClient | None = None
        self._manifest: list[dict] = []
        self._manifest_at: float = 0.0
        self._available: bool | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                timeout=self.config.timeout,
            )
        return self._client

    def _sign(self, body: bytes) -> dict[str, str]:
        if not self.config.shared_secret:
            return {}
        timestamp = str(int(time.time()))
        message = timestamp.encode("utf-8") + b"." + body
        signature = hmac.new(
            self.config.shared_secret.encode("utf-8"), message, hashlib.sha256
        ).hexdigest()
        return {
            "X-Harness-Timestamp": timestamp,
            "X-Harness-Signature": signature,
        }

    async def list_tools(self, *, force: bool = False) -> list[dict]:
        if not self.enabled:
            return []
        if not force and self._manifest and (time.time() - self._manifest_at) < 60:
            return self._manifest
        try:
            response = await self.client.get(self.config.manifest_path)
            response.raise_for_status()
            data = response.json()
            tools = data.get("tools", data) if isinstance(data, dict) else data
            if not isinstance(tools, list):
                raise ValueError("manifest is not a list")
            self._manifest = [item for item in tools if isinstance(item, dict) and item.get("name")]
            self._manifest_at = time.time()
            self._available = True
            logger.info(f"[JavaBridge] loaded {len(self._manifest)} tools")
        except Exception as exc:
            self._available = False
            self._manifest = []
            logger.warning(f"[JavaBridge] manifest unavailable: {exc}")
        return self._manifest

    async def invoke(
        self,
        name: str,
        arguments: dict,
        *,
        run_id: str = "",
        user_id: int | None = None,
    ) -> dict:
        if not self.enabled:
            return {"success": False, "error": "java bridge disabled"}
        payload = {
            "tool": name,
            "arguments": arguments,
            "run_id": run_id,
            "user_id": user_id,
            "ts": int(time.time()),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **self._sign(body)}
        try:
            response = await self.client.post(
                f"{self.config.manifest_path.rstrip('/')}/{name}/invoke",
                content=body,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            self._available = True
            return data if isinstance(data, dict) else {"success": True, "data": data}
        except Exception as exc:
            self._available = False
            logger.warning(f"[JavaBridge] invoke {name} failed: {exc}")
            return {"success": False, "error": str(exc)}

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "base_url": self.config.base_url,
            "available": self._available,
            "cached_tools": len(self._manifest),
            "signed": bool(self.config.shared_secret),
        }

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


java_bridge = JavaToolBridge()
