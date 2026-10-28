"""数据采集子 Agent —— 把公开网页/公告/新闻转成可继续分析的数据资产。

流程：
1. 根据任务输入或目标生成查询（或直接使用给定 URL）；
2. `web_search` 发现候选来源；
3. `web_fetch` 抓取正文（SSRF/域名/端口/大小/超时限制）；
4. 原始正文写入 `/logs/web/*.txt`，结构化记录写入 `/workspace/data/*.json`；
5. 生成 `/reports/data_collection_*.md` 报告，只向主 Agent 回传摘要与路径。

离线/无搜索密钥时：若未提供 URL，回退到内部规则库生成数据集，并在报告中明确标注，
避免任务空转，也不伪造“已抓取网页”。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from harness.agents.base import AgentContext, BaseAgent
from harness.agents.subagents.common import write_report
from harness.contracts import ArtifactRef
from harness.tools import risk_data
from server.utils.logger import logger


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DataCollectionAgent(BaseAgent):
    name = "data_collection"
    description = (
        "负责外部数据采集：搜索公开来源、抓取网页正文、抽取结构化记录，"
        "落 VFS 数据集与报告，供后续分析或审批材料引用。"
    )
    preferred_skills = ["data-collection-sop", "incident-triage"]

    def system_prompt(self, ctx: AgentContext, skill_hint: str, memory_hint: str) -> str:
        base = super().system_prompt(ctx, skill_hint, memory_hint)
        return base + (
            "\n\n数据采集要求：\n"
            "- 先 web_search 找候选来源，再 web_fetch 抓正文；已有 URL 时可直接抓取；\n"
            "- 每条记录保留 url、title、抓取时间、正文摘要与原始快照路径；\n"
            "- 用 dataset_write 落 /workspace/data，用 report_write 写采集报告；\n"
            "- 不抓取登录后、付费、隐私或明确禁止抓取的页面；失败要记录原因；\n"
            "- 最终结论必须给出数据集路径、成功来源数与主要限制。"
        )

    async def heuristic(
        self, ctx: AgentContext
    ) -> tuple[str, list[str], list[ArtifactRef]]:
        cfg = ctx.config.web
        inputs = ctx.task.inputs or {}
        queries = _queries_from(ctx)
        max_sources = _clamp_int(
            inputs.get("max_sources", cfg.max_sources), 1, cfg.max_sources
        )
        urls = _urls_from(inputs, max_sources)

        search_available: bool | None = None
        failures: list[dict[str, str]] = []

        if not urls and cfg.enabled:
            for query in queries[:3]:
                output, _, _ = await self._tool_call(
                    ctx, "web_search", {"query": query, "count": max_sources}
                )
                if isinstance(output, dict):
                    search_available = output.get("available")
                    for item in (output.get("results") or [])[:max_sources]:
                        url = str(item.get("url", "")).strip()
                        if url:
                            urls.append(url)
                urls = list(dict.fromkeys(urls))[:max_sources]
                if len(urls) >= max_sources:
                    break

        records: list[dict[str, Any]] = []
        raw_paths: list[str] = []
        for index, url in enumerate(urls[:max_sources]):
            output, _, _ = await self._tool_call(
                ctx, "web_fetch", {"url": url, "max_chars": min(cfg.max_chars, 8000)}
            )
            if not isinstance(output, dict) or not output.get("success"):
                failures.append(
                    {
                        "url": url,
                        "error": str((output or {}).get("error", "fetch failed"))[:300],
                    }
                )
                continue

            text = str(output.get("text", ""))
            raw_path = f"/logs/web/{ctx.task.seq}_{index}.txt"
            try:
                await ctx.vfs.write(
                    raw_path,
                    (
                        f"URL: {url}\n"
                        f"FINAL: {output.get('final_url', '')}\n"
                        f"TITLE: {output.get('title', '')}\n"
                        f"FETCHED_AT: {_now()}\n\n{text}"
                    ),
                    author=self.name,
                    comment="web snapshot",
                )
                raw_paths.append(raw_path)
            except Exception as exc:
                logger.debug(f"[DataCollection] raw snapshot failed: {exc}")

            records.append(
                {
                    "source": "web",
                    "query": queries[0] if queries else "",
                    "title": str(output.get("title", ""))[:200],
                    "url": url,
                    "final_url": str(output.get("final_url", "")),
                    "status_code": int(output.get("status_code", 0) or 0),
                    "content_type": str(output.get("content_type", "")),
                    "chars": len(text),
                    "excerpt": text[:600],
                    "fetched_at": _now(),
                    "raw_path": raw_path,
                }
            )

        mode = "web"
        if not records and cfg.offline_fallback:
            mode = "internal_rules"
            for rule in risk_data.list_rules():
                records.append(
                    {
                        "source": "internal_rules",
                        "type": "rule",
                        "rule_id": rule.get("rule_id", ""),
                        "name": rule.get("name", ""),
                        "active_version": rule.get("active_version"),
                        "gray_percent": rule.get("gray_percent"),
                        "owner": rule.get("owner", ""),
                        "status": rule.get("status", ""),
                        "fetched_at": _now(),
                    }
                )
        elif not records:
            mode = "empty"

        dataset_meta = {
            "objective": ctx.objective,
            "task_id": ctx.task.id,
            "queries": queries,
            "mode": mode,
            "search_available": search_available,
            "sources_attempted": len(urls),
            "sources_succeeded": sum(1 for r in records if r.get("source") == "web"),
            "failures": failures,
            "generated_at": _now(),
        }
        dataset_output, _, dataset_artifacts = await self._tool_call(
            ctx,
            "dataset_write",
            {
                "name": f"dataset_{ctx.task.seq}",
                "records": records,
                "format": "json",
                "metadata": dataset_meta,
            },
        )
        dataset_path = (
            str(dataset_output.get("path", ""))
            if isinstance(dataset_output, dict)
            else ""
        )

        report_body = _build_report(
            ctx=ctx,
            queries=queries,
            records=records,
            failures=failures,
            mode=mode,
            search_available=search_available,
            dataset_path=dataset_path,
            raw_paths=raw_paths,
            max_sources=max_sources,
        )
        report_path, report_artifact = await write_report(
            ctx,
            f"data_collection_{ctx.task.seq}.md",
            report_body,
            author=self.name,
            comment="数据采集报告",
        )

        web_count = sum(1 for r in records if r.get("source") == "web")
        summary = (
            f"数据采集完成（模式：{mode}）：共 {len(records)} 条记录，"
            f"网页来源 {web_count} 条，失败 {len(failures)} 个；"
            f"数据集 {dataset_path or '未生成'}；报告 {report_path}。"
        )
        findings = [
            f"查询：{' / '.join(queries[:3])}",
            f"候选 URL：{len(urls)}，抓取成功：{web_count}",
        ]
        if search_available is False:
            findings.append("外部搜索不可用（未配置密钥或已关闭），已按配置回退")
        if mode == "internal_rules":
            findings.append("未获取到网页数据，已回退到内部规则库数据集")
        findings.extend(
            f"失败：{item['url']}（{item['error']}）" for item in failures[:3]
        )
        if raw_paths:
            findings.append(f"原始快照：{raw_paths[0]} 等 {len(raw_paths)} 个文件")

        artifacts = [*dataset_artifacts, report_artifact]
        return summary, findings, artifacts


def _queries_from(ctx: AgentContext) -> list[str]:
    inputs = ctx.task.inputs or {}
    raw = inputs.get("queries")
    if isinstance(raw, list) and raw:
        return [str(item).strip() for item in raw if str(item).strip()][:5]
    query = str(inputs.get("query", "")).strip()
    if query:
        return [query]
    return [ctx.objective]


def _urls_from(inputs: dict, limit: int) -> list[str]:
    raw = inputs.get("urls")
    if not isinstance(raw, list):
        return []
    urls: list[str] = []
    for item in raw:
        value = str(item).strip()
        if value.startswith(("http://", "https://")):
            urls.append(value)
    return list(dict.fromkeys(urls))[:limit]


def _clamp_int(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(number, high))


def _build_report(
    *,
    ctx: AgentContext,
    queries: list[str],
    records: list[dict[str, Any]],
    failures: list[dict[str, str]],
    mode: str,
    search_available: bool | None,
    dataset_path: str,
    raw_paths: list[str],
    max_sources: int,
) -> str:
    rows = []
    for record in records[:20]:
        rows.append(
            "| {source} | {title} | {url} | {chars} |".format(
                source=record.get("source", ""),
                title=str(record.get("title") or record.get("name") or "")[:60],
                url=str(record.get("url") or record.get("rule_id") or "")[:120],
                chars=record.get("chars", "-"),
            )
        )
    table = "\n".join(
        ["| 来源类型 | 标题 | URL/ID | 字符数 |", "|---|---|---|---|", *rows]
    ) or "| - | - | - | - |"

    failure_lines = "\n".join(
        f"- `{item['url']}`：{item['error']}" for item in failures
    ) or "- 无"

    mode_note = {
        "web": "已抓取公开网页正文。",
        "internal_rules": (
            "外部搜索不可用且未提供可抓取 URL，已回退到内部规则库；"
            "本数据集不代表外部公开数据。"
        ),
        "empty": "未采集到任何数据，请检查网络、搜索密钥或输入 URL。",
    }.get(mode, mode)

    return "\n".join(
        [
            "# 数据采集报告",
            "",
            f"- 生成时间：{_now()}",
            f"- 目标：{ctx.objective}",
            f"- 任务：{ctx.task.title}（{ctx.task.id}）",
            f"- 采集模式：{mode}",
            f"- 查询：{' / '.join(queries[:5])}",
            f"- 最大来源数：{max_sources}",
            f"- 外部搜索可用：{search_available}",
            f"- 记录数：{len(records)}；失败数：{len(failures)}",
            "",
            "## 来源与数据",
            "",
            table,
            "",
            "## 数据集",
            f"- 路径：`{dataset_path or '未生成'}`",
            f"- 原始快照：{len(raw_paths)} 个（/logs/web/）",
            "",
            "## 失败与限制",
            failure_lines,
            "",
            "## 说明",
            f"- {mode_note}",
            "- 仅采集公开可访问内容；登录后、付费、隐私或明确禁止抓取的页面不应抓取。",
            "- 数据使用前需核对来源可信度、发布时间与口径；抓取内容可能被截断。",
            "- 原始网页文本已落 VFS，主 Agent 仅引用摘要与路径。",
        ]
    )
