"""数据采集工具 —— 搜索、抓取网页正文、落结构化数据集。"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from harness.contracts import RiskLevel
from harness.integrations.web import web_fetch_client
from harness.tools.base import INT, STR, HarnessTool, schema
from harness.tools.context import ToolContext
from server.utils.web_search import web_search_client


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "_", str(name or "")).strip("_")
    return (cleaned or "dataset")[:80]


def _to_csv(records: list[dict]) -> str:
    keys: list[str] = []
    for record in records:
        for key in record:
            if key not in keys:
                keys.append(key)
    if not keys:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=keys, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        row = {
            key: json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else value
            for key, value in record.items()
        }
        writer.writerow(row)
    return buffer.getvalue()


def build_web_tools(ctx: ToolContext) -> list[HarnessTool]:
    async def web_search(
        query: str, count: int = 5, freshness: str = "noLimit"
    ) -> dict:
        count = max(1, min(int(count or 5), 10))
        results = await web_search_client.search(
            query, count=count, freshness=freshness or "noLimit"
        )
        return {
            "query": query,
            "available": web_search_client.available,
            "count": len(results),
            "results": results,
        }

    async def web_fetch(url: str, max_chars: int = 8000) -> dict:
        try:
            limit = int(max_chars)
        except (TypeError, ValueError):
            limit = 8000
        result = await web_fetch_client.fetch(url, max_chars=limit)
        return result.to_dict()

    async def dataset_write(
        name: str,
        records: list[dict],
        format: str = "json",
        metadata: dict | None = None,
    ) -> dict:
        if not isinstance(records, list):
            return {"error": "records must be an array"}
        cleaned = [item for item in records if isinstance(item, dict)][:500]
        fmt = str(format or "json").lower()
        if fmt not in {"json", "csv"}:
            fmt = "json"
        safe = _safe_name(name)
        path = f"/workspace/data/{safe}.{fmt}"
        if fmt == "csv":
            content = _to_csv(cleaned)
        else:
            content = json.dumps(
                {"metadata": metadata or {}, "records": cleaned},
                ensure_ascii=False,
                indent=2,
            )
        entry = await ctx.vfs.write(
            path, content, author="data_collection", comment=f"dataset ({fmt})"
        )
        return {
            "path": path,
            "kind": "dataset",
            "count": len(cleaned),
            "format": fmt,
            "size": entry.size,
            "sha256": entry.sha256,
        }

    return [
        HarnessTool(
            name="web_search",
            description=(
                "联网搜索公开信息，返回标题、URL、摘要与发布时间。"
                "用于数据采集前发现候选来源；不抓取网页正文。"
            ),
            parameters=schema(
                {"query": STR, "count": INT, "freshness": STR},
                ["query"],
            ),
            handler=web_search,
            risk=RiskLevel.LOW,
            action="web.search",
            tags=["web", "data", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="web_fetch",
            description=(
                "抓取指定 URL 的网页正文（HTML 转文本），带 SSRF 防护、域名/端口限制、"
                "超时、大小截断与缓存。仅用于公开可访问页面。"
            ),
            parameters=schema({"url": STR, "max_chars": INT}, ["url"]),
            handler=web_fetch,
            risk=RiskLevel.MEDIUM,
            action="web.fetch",
            tags=["web", "data", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="dataset_write",
            description=(
                "把采集到的结构化记录写入 /workspace/data 下的 JSON 或 CSV 数据集，"
                "自动留版本；返回文件路径、条数与大小。"
            ),
            parameters=schema(
                {
                    "name": STR,
                    "records": {"type": "array", "items": {"type": "object"}},
                    "format": {"type": "string", "enum": ["json", "csv"]},
                    "metadata": {"type": "object"},
                },
                ["name", "records"],
            ),
            handler=dataset_write,
            risk=RiskLevel.LOW,
            action="file.write",
            tags=["data", "write"],
        ),
    ]
