"""网页抓取客户端 —— 数据采集 Agent 的取数底座。

安全边界（默认全部开启）：
- 只允许 http/https，拒绝 userinfo、非 80/443 端口；
- 域名白名单/黑名单；
- DNS 解析后拒绝回环、私网、链路本地、保留、组播地址（SSRF 防护）；
- 手动跟随重定向并逐跳复验（防止 302 跳到内网）；
- 超时、最大字节、最大字符、内容类型白名单；
- 抓取结果缓存（Redis 优先，进程内兜底）。

注意：DNS 校验与实际连接之间存在 rebinding 时间窗，生产环境应叠加出口代理/
网络策略；本模块是应用层纵深防御，不替代网络隔离。
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from harness.config import WebConfig, get_harness_config
from server.utils.logger import logger
from server.utils.redis_client import redis_client

CACHE_PREFIX = "harness:web:fetch:"

ALLOWED_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/json",
    "application/xml",
    "text/xml",
    "text/csv",
}


class WebFetchError(Exception):
    pass


@dataclass
class FetchResult:
    success: bool
    url: str
    final_url: str = ""
    status_code: int = 0
    title: str = ""
    text: str = ""
    content_type: str = ""
    bytes_read: int = 0
    truncated: bool = False
    cached: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class _TextExtractor(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "object"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        else:
            self.parts.append(data)


def normalize_text(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def extract_html(html: str) -> tuple[str, str]:
    """返回 (title, text)；去掉 script/style 等噪声。"""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # HTML 畸形时退化为正则去标签
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return "", normalize_text(text)
    return normalize_text(parser.title), normalize_text(" ".join(parser.parts))


class WebFetchClient:
    def __init__(
        self,
        config: WebConfig | None = None,
        client: httpx.AsyncClient | None = None,
        *,
        validate_dns: bool = True,
    ) -> None:
        self.config = config or get_harness_config().web
        self._client = client
        self._validate_dns = validate_dns
        self._local_cache: dict[str, tuple[float, dict]] = {}
        self._cache_limit = 128

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.config.timeout,
                headers={"User-Agent": self.config.user_agent},
                follow_redirects=False,
            )
        return self._client

    # ------------------------------------------------------------------ public
    async def fetch(self, url: str, *, max_chars: int | None = None) -> FetchResult:
        if not self.config.enabled or not self.config.fetch_enabled:
            return FetchResult(False, url, error="web fetch disabled")

        try:
            normalized = await self._validate_url(url)
        except WebFetchError as exc:
            return FetchResult(False, url, error=str(exc))

        limit = min(
            max_chars or self.config.max_chars,
            self.config.max_chars,
        )
        cache_key = self._cache_key(normalized, limit)
        cached = await self._cache_get(cache_key)
        if cached is not None:
            cached["cached"] = True
            return FetchResult(**cached)

        try:
            result = await self._fetch_once(normalized, limit)
        except WebFetchError as exc:
            return FetchResult(False, url, error=str(exc))
        except Exception as exc:
            logger.warning(f"[WebFetch] {url} failed: {exc}")
            return FetchResult(False, url, error=f"{type(exc).__name__}: {exc}")

        if result.success:
            await self._cache_set(cache_key, result.to_dict())
        return result

    # --------------------------------------------------------------- fetch impl
    async def _fetch_once(self, url: str, max_chars: int) -> FetchResult:
        current = url
        for _ in range(max(0, self.config.max_redirects) + 1):
            current = await self._validate_url(current)
            try:
                async with self.client.stream("GET", current) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        location = response.headers.get("location")
                        if not location:
                            return FetchResult(
                                False, url, status_code=status,
                                error="redirect without location",
                            )
                        current = urljoin(current, location)
                        continue

                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    if content_type and content_type not in ALLOWED_CONTENT_TYPES:
                        return FetchResult(
                            False, url, status_code=status,
                            content_type=content_type,
                            error=f"unsupported content-type: {content_type}",
                        )

                    raw = bytearray()
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        remaining = self.config.max_bytes - len(raw)
                        if remaining <= 0:
                            truncated = True
                            break
                        if len(chunk) > remaining:
                            raw.extend(chunk[:remaining])
                            truncated = True
                            break
                        raw.extend(chunk)

                    try:
                        encoding = response.encoding or "utf-8"
                    except Exception:
                        encoding = "utf-8"
                    body = bytes(raw).decode(encoding, errors="replace")
                    if content_type in {"text/html", "application/xhtml+xml"}:
                        title, text = extract_html(body)
                    else:
                        title, text = "", body
                    text = normalize_text(text)[:max_chars]

                    return FetchResult(
                        success=True,
                        url=url,
                        final_url=str(response.url),
                        status_code=status,
                        title=title[:300],
                        text=text,
                        content_type=content_type,
                        bytes_read=len(raw),
                        truncated=truncated,
                    )
            except httpx.TimeoutException:
                return FetchResult(False, url, error=f"timeout after {self.config.timeout}s")
            except httpx.HTTPError as exc:
                return FetchResult(False, url, error=f"http error: {exc}")

        return FetchResult(False, url, error="too many redirects")

    # ---------------------------------------------------------------- security
    async def _validate_url(self, url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            raise WebFetchError("empty url")
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise WebFetchError(f"unsupported scheme: {parsed.scheme}")
        if parsed.username or parsed.password:
            raise WebFetchError("userinfo in url is not allowed")
        if not parsed.hostname:
            raise WebFetchError("missing hostname")

        host = parsed.hostname.lower().rstrip(".")
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise WebFetchError("invalid hostname")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            raise WebFetchError("invalid port")
        if port not in self.config.allowed_ports:
            raise WebFetchError(f"port {port} is not allowed")

        if self._matches(ascii_host, self.config.blocked_domains):
            raise WebFetchError(f"domain blocked: {ascii_host}")
        if self.config.allowed_domains and not self._matches(
            ascii_host, self.config.allowed_domains
        ):
            raise WebFetchError(f"domain not in allowlist: {ascii_host}")

        if self._validate_dns:
            await asyncio.to_thread(self._assert_public_host, ascii_host)

        default_port = 443 if parsed.scheme.lower() == "https" else 80
        netloc = ascii_host if port == default_port else f"{ascii_host}:{port}"
        return urlunsplit(
            (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
        )

    @staticmethod
    def _matches(host: str, domains: list[str]) -> bool:
        for domain in domains:
            candidate = domain.lower().strip().lstrip(".").rstrip(".")
            if candidate and (host == candidate or host.endswith("." + candidate)):
                return True
        return False

    @staticmethod
    def _assert_public_host(host: str) -> None:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise WebFetchError(f"dns resolution failed: {host}") from exc
        if not infos:
            raise WebFetchError(f"dns resolution failed: {host}")
        for info in infos:
            address = str(info[4][0]).split("%")[0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                raise WebFetchError(f"invalid resolved address: {address}")
            if not ip.is_global:
                raise WebFetchError(
                    f"refusing non-public address {address} for host {host}"
                )

    # ------------------------------------------------------------------ cache
    def _cache_key(self, url: str, max_chars: int) -> str:
        digest = hashlib.sha256(f"{url}|{max_chars}".encode("utf-8")).hexdigest()
        return f"{CACHE_PREFIX}{digest}"

    async def _cache_get(self, key: str) -> dict | None:
        client = redis_client.client
        if client is not None:
            try:
                raw = await client.get(key)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        entry = self._local_cache.get(key)
        if entry and entry[0] > time.time():
            return dict(entry[1])
        return None

    async def _cache_set(self, key: str, payload: dict) -> None:
        data = dict(payload)
        data["cached"] = False
        client = redis_client.client
        if client is not None:
            try:
                await client.set(
                    key,
                    json.dumps(data, ensure_ascii=False),
                    ex=self.config.cache_ttl,
                )
                return
            except Exception:
                pass
        if len(self._local_cache) >= self._cache_limit:
            self._local_cache.pop(next(iter(self._local_cache)))
        self._local_cache[key] = (time.time() + self.config.cache_ttl, data)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


web_fetch_client = WebFetchClient()
