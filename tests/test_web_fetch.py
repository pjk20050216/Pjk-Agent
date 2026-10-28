"""网页抓取安全与解析：SSRF 防护、HTML 正文、内容类型、缓存、重定向。"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

import httpx

from harness.integrations.web import (
    WebFetchClient,
    WebFetchError,
    extract_html,
)
from tests.base import make_config


class WebFetchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="webfetch-test-"))
        self.cfg = make_config(self.root)
        self.cfg.web.cache_ttl = 60

    def test_extract_html(self) -> None:
        html = """
        <html><head><title>监管公告</title>
        <style>.x{color:red}</style></head>
        <body><h1>支付风控新规</h1><script>alert('x')</script>
        <p>自 2026 年起执行。</p></body></html>
        """
        title, text = extract_html(html)
        self.assertEqual(title, "监管公告")
        self.assertIn("支付风控新规", text)
        self.assertIn("自 2026 年起执行", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("color:red", text)

    async def test_ssrf_guard_rejects_private_and_non_http(self) -> None:
        client = WebFetchClient(self.cfg.web, validate_dns=True)
        for url in (
            "file:///etc/passwd",
            "ftp://example.com/a",
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://169.254.1.1/",
            "http://example.com:8080/",
            "http://user:pass@example.com/",
        ):
            with self.assertRaises(WebFetchError, msg=url):
                await client._validate_url(url)

    async def test_fetch_html_and_cache(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=(
                    "<html><head><title>Article</title></head>"
                    "<body><p>hello harness</p><script>bad()</script></body></html>"
                ).encode("utf-8"),
            )

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport, follow_redirects=False)
        client = WebFetchClient(self.cfg.web, http_client, validate_dns=False)
        url = f"https://example.com/{uuid.uuid4().hex}"

        first = await client.fetch(url)
        self.assertTrue(first.success, first.error)
        self.assertEqual(first.title, "Article")
        self.assertIn("hello harness", first.text)
        self.assertNotIn("bad()", first.text)
        self.assertFalse(first.cached)

        second = await client.fetch(url)
        self.assertTrue(second.success)
        self.assertTrue(second.cached)
        self.assertEqual(calls["n"], 1)
        await http_client.aclose()

    async def test_rejects_redirect_to_private(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/secret"},
            )

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport, follow_redirects=False)
        client = WebFetchClient(self.cfg.web, http_client, validate_dns=True)
        result = await client.fetch("http://93.184.216.34/")
        self.assertFalse(result.success)
        self.assertIn("blocked", (result.error or "").lower())
        await http_client.aclose()

    async def test_rejects_binary_content_type(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=b"\x00\x01\x02",
            )

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport, follow_redirects=False)
        client = WebFetchClient(self.cfg.web, http_client, validate_dns=False)
        result = await client.fetch("https://example.com/file.bin")
        self.assertFalse(result.success)
        self.assertIn("content-type", (result.error or ""))
        await http_client.aclose()


if __name__ == "__main__":
    unittest.main()
