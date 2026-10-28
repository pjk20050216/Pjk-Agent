"""子 Agent 共用工具函数。"""
from __future__ import annotations

import re
from typing import Iterable

from harness.agents.base import AgentContext
from harness.contracts import ArtifactRef
from harness.tools import risk_data

RULE_ID_RE = re.compile(r"R_[A-Z]+_\d+")

DEFAULT_FEATURES = [
    "device_risk_score",
    "login_fail_count_1h",
    "same_device_user_count_7d",
    "user_age_days",
]


def rule_id_from(ctx: AgentContext, default: str = "R_RISK_001") -> str:
    inputs = ctx.task.inputs or {}
    explicit = str(inputs.get("rule_id", "")).strip()
    if explicit:
        return explicit
    match = RULE_ID_RE.search(ctx.objective) or RULE_ID_RE.search(ctx.task.description)
    if match:
        return match.group(0)
    return default


def features_from(ctx: AgentContext) -> list[str]:
    inputs = ctx.task.inputs or {}
    explicit = inputs.get("features")
    if isinstance(explicit, list) and explicit:
        return [str(item) for item in explicit]
    known = [name for name in risk_data.FEATURES if name in ctx.objective]
    if known:
        return known
    rule = risk_data.get_rule(rule_id_from(ctx))
    if rule:
        return list(rule["depends_on"])
    return DEFAULT_FEATURES


def find_features_in_text(text: str) -> list[str]:
    return [name for name in risk_data.FEATURES if name in text]


async def write_report(
    ctx: AgentContext,
    filename: str,
    body: str,
    *,
    author: str,
    comment: str = "report",
) -> tuple[str, ArtifactRef]:
    path = filename if filename.startswith("/") else f"/reports/{filename}"
    entry = await ctx.vfs.write(path, body, author=author, comment=comment)
    artifact = ArtifactRef(
        path=entry.path,
        kind="report",
        summary=comment,
        size=entry.size,
        sha256=entry.sha256,
        created_by=author,
    )
    return path, artifact


def markdown_table(headers: Iterable[str], rows: Iterable[Iterable[object]]) -> str:
    header = "| " + " | ".join(str(h) for h in headers) + " |"
    divider = "|" + "|".join(["---"] * len(list(headers))) + "|"
    body = "\n".join(
        "| " + " | ".join(str(cell) for cell in row) + " |" for row in rows
    )
    return "\n".join([header, divider, body])
