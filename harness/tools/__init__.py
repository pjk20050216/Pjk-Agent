"""工具集装配 —— 按 Agent 角色裁剪可见工具，降低上下文与误用风险。"""
from __future__ import annotations

from harness.tools.base import HarnessTool, ToolRegistry
from harness.tools.builtin import build_builtin_tools
from harness.tools.context import ToolContext
from harness.tools.domain import build_domain_tools
from harness.tools.java import build_java_tools
from harness.tools.web import build_web_tools

FS_READ = {"fs_read", "fs_list", "fs_search", "fs_history", "fs_diff"}
FS_WRITE = {"fs_write", "fs_edit", "report_write", "log_write"}
SKILLS = {"skill_list", "skill_view", "skill_resource", "skill_match"}
MEMORY = {"memory_search", "memory_remember", "memory_recent"}

MAIN_TOOLSET = {
    "task_list", "task_get",
    *FS_READ, *FS_WRITE, *SKILLS, *MEMORY,
    "rule_list", "rule_get", "rule_diff", "rule_validate", "rule_apply",
    "feature_query", "feature_stats",
    "log_search", "knowledge_search", "approval_request", "sandbox_status",
}

SUBAGENT_TOOLSETS: dict[str, set[str]] = {
    "feature_analysis": {
        "feature_query", "feature_stats", "rule_get",
        "fs_read", "fs_write", "fs_list", "fs_search",
        "report_write", "skill_view", "skill_resource", "skill_match",
        "memory_search",
    },
    "rule_config": {
        "rule_list", "rule_get", "rule_diff", "rule_validate", "rule_apply",
        "rule_publish", "rule_gray_release", "rule_rollback",
        "feature_stats",
        "fs_read", "fs_write", "fs_edit", "fs_search",
        "report_write", "skill_view", "skill_resource", "skill_match",
        "memory_search",
    },
    "test_validation": {
        "rule_get", "rule_diff", "rule_backtest", "feature_query",
        "fs_read", "fs_write", "fs_list", "fs_search",
        "report_write", "log_write",
        "sandbox_write", "sandbox_read", "sandbox_exec", "sandbox_promote",
        "skill_view", "skill_resource", "memory_search",
    },
    "anomaly_diagnosis": {
        "log_search", "rule_get", "rule_diff", "feature_query", "feature_stats",
        "knowledge_search",
        "fs_read", "fs_write", "fs_list", "fs_search",
        "report_write", "log_write",
        "sandbox_write", "sandbox_read", "sandbox_exec",
        "skill_view", "skill_resource", "memory_search",
    },
    "knowledge_retrieval": {
        "knowledge_search", "memory_search", "memory_remember", "memory_recent",
        "skill_list", "skill_view", "skill_resource", "skill_match",
        "fs_read", "fs_write", "report_write",
    },
    "data_collection": {
        "web_search", "web_fetch", "dataset_write",
        "fs_read", "fs_write", "fs_list", "fs_search",
        "report_write", "log_write",
        "skill_view", "skill_resource", "skill_match",
        "memory_search", "knowledge_search",
    },
}


async def build_registry(
    ctx: ToolContext,
    allow: set[str] | None = None,
) -> ToolRegistry:
    tools: list[HarnessTool] = [
        *build_builtin_tools(ctx),
        *build_domain_tools(ctx),
        *build_web_tools(ctx),
    ]
    if ctx.java_bridge is not None and getattr(ctx.java_bridge, "enabled", False):
        try:
            tools.extend(await build_java_tools(ctx, ctx.java_bridge))
        except Exception:
            pass
    registry = ToolRegistry(tools)
    return registry.subset(allow) if allow else registry
