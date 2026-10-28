"""通用工具 —— 虚拟文件系统、Skills、沙箱、记忆、任务清单。

这些工具构成所有 Agent 的“基础操作面”：读写工作资产、按需加载技能、
在隔离沙箱执行脚本、检索长期经验、查看任务进度。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from harness.contracts import FilePlane, MemoryRecord, RiskLevel
from harness.tools.base import BOOL, INT, STR, HarnessTool, schema
from harness.tools.context import ToolContext


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def build_fs_tools(ctx: ToolContext) -> list[HarnessTool]:
    vfs = ctx.vfs

    async def fs_read(path: str, max_chars: int = 8000) -> dict:
        content = await vfs.read(path)
        truncated = len(content) > max_chars
        return {
            "path": path,
            "content": content[:max_chars],
            "truncated": truncated,
            "size": len(content),
        }

    async def fs_write(path: str, content: str, comment: str = "") -> dict:
        entry = await vfs.write(path, content, author="agent", comment=comment)
        return entry.model_dump(mode="json")

    async def fs_edit(
        path: str, old: str, new: str, replace_all: bool = False
    ) -> dict:
        entry = await vfs.edit(
            path, old, new, replace_all=replace_all, author="agent"
        )
        return entry.model_dump(mode="json")

    async def fs_list(path: str = "/workspace", recursive: bool = False) -> dict:
        entries = await vfs.list_dir(path, recursive=recursive)
        return {
            "path": path,
            "entries": [e.model_dump(mode="json") for e in entries],
            "count": len(entries),
        }

    async def fs_search(
        query: str, path: str = "/", regex: bool = False, limit: int = 30
    ) -> dict:
        hits = await vfs.search(query, path=path, regex=regex, limit=limit)
        return {
            "query": query,
            "hits": [h.model_dump(mode="json") for h in hits],
            "count": len(hits),
        }

    async def fs_history(path: str) -> dict:
        versions = await vfs.history(path)
        return {
            "path": path,
            "versions": [v.model_dump(mode="json") for v in versions],
        }

    async def fs_diff(path: str, from_version: int, to_version: int) -> dict:
        return {
            "path": path,
            "diff": await vfs.diff(path, from_version, to_version),
        }

    async def report_write(name: str, content: str) -> dict:
        filename = name if name.endswith((".md", ".txt")) else f"{name}.md"
        path = f"/reports/{filename}"
        entry = await vfs.write(path, content, author="agent", comment="report")
        return entry.model_dump(mode="json")

    async def log_write(name: str, content: str) -> dict:
        filename = name if name.endswith((".log", ".txt")) else f"{name}.log"
        path = f"/logs/{filename}"
        entry = await vfs.write(path, content, author="agent", comment="log snapshot")
        return entry.model_dump(mode="json")

    return [
        HarnessTool(
            name="fs_read",
            description="读取虚拟文件系统中的文本文件，返回内容（超长自动截断）。",
            parameters=schema(
                {"path": STR, "max_chars": INT},
                ["path"],
            ),
            handler=fs_read,
            tags=["fs", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="fs_write",
            description="写入或覆盖虚拟文件系统文件，自动保留版本。路径必须以 /workspace /reports /logs /policies /skills /memories 开头。",
            parameters=schema(
                {"path": STR, "content": STR, "comment": STR},
                ["path", "content"],
            ),
            handler=fs_write,
            action="file.write",
            tags=["fs", "write"],
        ),
        HarnessTool(
            name="fs_edit",
            description="对已有文件做精确文本替换（默认只替换第一处），并生成新版本。",
            parameters=schema(
                {"path": STR, "old": STR, "new": STR, "replace_all": BOOL},
                ["path", "old", "new"],
            ),
            handler=fs_edit,
            action="file.write",
            tags=["fs", "write"],
        ),
        HarnessTool(
            name="fs_list",
            description="列出目录内容（文件/子目录、大小、版本号）。",
            parameters=schema(
                {"path": STR, "recursive": BOOL},
                ["path"],
            ),
            handler=fs_list,
            tags=["fs", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="fs_search",
            description="在虚拟文件系统中按关键字或正则搜索，返回文件路径、行号与命中行。",
            parameters=schema(
                {"query": STR, "path": STR, "regex": BOOL, "limit": INT},
                ["query"],
            ),
            handler=fs_search,
            tags=["fs", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="fs_history",
            description="查看文件的版本历史（版本号、作者、时间、变更说明）。",
            parameters=schema({"path": STR}, ["path"]),
            handler=fs_history,
            tags=["fs", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="fs_diff",
            description="对比同一文件两个版本的 unified diff。",
            parameters=schema(
                {"path": STR, "from_version": INT, "to_version": INT},
                ["path", "from_version", "to_version"],
            ),
            handler=fs_diff,
            tags=["fs", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="report_write",
            description="把报告写入 /reports 目录（自动补 .md 后缀并留痕）。",
            parameters=schema({"name": STR, "content": STR}, ["name", "content"]),
            handler=report_write,
            action="file.write",
            tags=["fs", "report"],
        ),
        HarnessTool(
            name="log_write",
            description="把日志快照写入 /logs 目录。",
            parameters=schema({"name": STR, "content": STR}, ["name", "content"]),
            handler=log_write,
            action="file.write",
            tags=["fs", "log"],
        ),
    ]


def build_skill_tools(ctx: ToolContext) -> list[HarnessTool]:
    async def skill_list() -> dict:
        return {
            "skills": [m.model_dump(mode="json") for m in ctx.skills.list_meta()],
            "count": len(ctx.skills),
        }

    async def skill_view(name: str) -> dict:
        try:
            body = ctx.skills.load_body(name)
        except KeyError:
            return {"error": f"unknown skill: {name}"}
        meta = ctx.skills.get(name)
        return {
            "name": name,
            "description": meta.meta.description if meta else "",
            "body": body,
            "resources": meta.meta.resources if meta else [],
        }

    async def skill_resource(name: str, path: str) -> dict:
        try:
            content = ctx.skills.load_resource(name, path)
        except KeyError:
            return {"error": f"unknown skill: {name}"}
        except (FileNotFoundError, PermissionError) as exc:
            return {"error": str(exc)}
        return {"name": name, "path": path, "content": content}

    async def skill_match(query: str, limit: int = 3) -> dict:
        matches = ctx.skills.match(query, limit=limit)
        return {"matches": [m.model_dump(mode="json") for m in matches]}

    return [
        HarnessTool(
            name="skill_list",
            description="列出可用技能（仅元数据，渐进式披露第一级）。",
            parameters=schema({}),
            handler=skill_list,
            tags=["skill", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="skill_view",
            description="加载指定技能的完整 SOP 正文（渐进式披露第二级）。",
            parameters=schema({"name": STR}, ["name"]),
            handler=skill_view,
            tags=["skill", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="skill_resource",
            description="读取技能目录下的资源文件，如模板、SQL、领域文档（第三级）。",
            parameters=schema({"name": STR, "path": STR}, ["name", "path"]),
            handler=skill_resource,
            tags=["skill", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="skill_match",
            description="按问题描述匹配最相关的技能，返回技能元数据。",
            parameters=schema({"query": STR, "limit": INT}, ["query"]),
            handler=skill_match,
            tags=["skill", "read"],
            cacheable=True,
        ),
    ]


def build_sandbox_tools(ctx: ToolContext) -> list[HarnessTool]:
    async def sandbox_exec(command: str, cwd: str = "workspace", timeout: int | None = None) -> dict:
        result = await ctx.sandbox.exec(ctx.run_id, command, cwd=cwd, timeout=timeout)
        log_name = f"sandbox_{_now_tag()}.log"
        await ctx.vfs.write(
            f"/logs/{log_name}",
            f"$ {command}\n\n[stdout]\n{result.stdout}\n\n[stderr]\n{result.stderr}\n",
            author="sandbox",
            comment="sandbox exec output",
        )
        payload = result.model_dump(mode="json")
        payload["log_path"] = f"/logs/{log_name}"
        return payload

    async def sandbox_write(path: str, content: str) -> dict:
        entry = await ctx.vfs.write(
            path, content, author="sandbox", comment="sandbox file", plane=FilePlane.SANDBOX
        )
        try:
            await ctx.sandbox.write_file(ctx.run_id, path.lstrip("/"), content)
        except Exception:
            pass
        return entry.model_dump(mode="json")

    async def sandbox_read(path: str) -> dict:
        try:
            content = await ctx.sandbox.read_file(ctx.run_id, path.lstrip("/"))
        except Exception:
            content = await ctx.vfs.read(path, plane=FilePlane.SANDBOX)
        return {"path": path, "content": content}

    async def sandbox_promote(sandbox_path: str, host_path: str) -> dict:
        try:
            content = await ctx.sandbox.read_file(ctx.run_id, sandbox_path.lstrip("/"))
        except Exception:
            content = await ctx.vfs.read(sandbox_path, plane=FilePlane.SANDBOX)
        entry = await ctx.vfs.write(
            host_path,
            content,
            author="sandbox",
            comment=f"promote from {sandbox_path}",
        )
        return entry.model_dump(mode="json")

    async def sandbox_status() -> dict:
        return ctx.sandbox.status(ctx.run_id)

    return [
        HarnessTool(
            name="sandbox_exec",
            description="在隔离沙箱执行脚本/命令（工作目录为沙箱 workspace），输出写入 /logs。高风险动作，需人工审批。",
            parameters=schema(
                {"command": STR, "cwd": STR, "timeout": INT},
                ["command"],
            ),
            handler=sandbox_exec,
            risk=RiskLevel.HIGH,
            action="sandbox.exec",
            tags=["sandbox", "exec"],
            requires_approval=True,
        ),
        HarnessTool(
            name="sandbox_write",
            description="在沙箱平面写入脚本文件（不会进入主工作区）。",
            parameters=schema({"path": STR, "content": STR}, ["path", "content"]),
            handler=sandbox_write,
            risk=RiskLevel.MEDIUM,
            action="sandbox.write",
            tags=["sandbox", "write"],
        ),
        HarnessTool(
            name="sandbox_read",
            description="读取沙箱平面文件。",
            parameters=schema({"path": STR}, ["path"]),
            handler=sandbox_read,
            action="sandbox.read",
            tags=["sandbox", "read"],
        ),
        HarnessTool(
            name="sandbox_promote",
            description="把沙箱产物晋升到主工作区（需人工审批）。",
            parameters=schema(
                {"sandbox_path": STR, "host_path": STR},
                ["sandbox_path", "host_path"],
            ),
            handler=sandbox_promote,
            risk=RiskLevel.HIGH,
            action="file.promote",
            tags=["sandbox", "write"],
            requires_approval=True,
        ),
        HarnessTool(
            name="sandbox_status",
            description="查看当前运行的沙箱 provider、策略与会话状态。",
            parameters=schema({}),
            handler=sandbox_status,
            tags=["sandbox", "read"],
            cacheable=False,
        ),
    ]


def build_memory_tools(ctx: ToolContext) -> list[HarnessTool]:
    async def memory_search(query: str, kind: str | None = None, top_k: int = 5) -> dict:
        records = await ctx.memory.recall(
            query, user_id=ctx.user_id, top_k=top_k, kind=kind
        )
        return {
            "query": query,
            "memories": [r.model_dump(mode="json") for r in records],
            "count": len(records),
        }

    async def memory_remember(text: str, kind: str = "experience", tags: list[str] | None = None) -> dict:
        record = MemoryRecord(
            kind=kind,
            text=text,
            tags=tags or [],
            run_id=ctx.run_id,
            user_id=ctx.user_id,
            metadata={"source": "agent"},
        )
        stored = await ctx.memory.remember([record])
        return {"stored": stored, "id": record.id, "kind": kind}

    async def memory_recent(limit: int = 10, kind: str | None = None) -> dict:
        records = await ctx.memory.recent(limit=limit, kind=kind)
        return {"memories": [r.model_dump(mode="json") for r in records]}

    return [
        HarnessTool(
            name="memory_search",
            description="检索长期记忆（历史排查经验、策略模板、用户偏好、历史案例）。",
            parameters=schema({"query": STR, "kind": STR, "top_k": INT}, ["query"]),
            handler=memory_search,
            tags=["memory", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="memory_remember",
            description="把一条可复用经验写入长期记忆（kind: experience/template/preference/case）。",
            parameters=schema(
                {"text": STR, "kind": STR, "tags": {"type": "array", "items": STR}},
                ["text"],
            ),
            handler=memory_remember,
            action="memory.write",
            tags=["memory", "write"],
        ),
        HarnessTool(
            name="memory_recent",
            description="查看最近沉淀的长期记忆。",
            parameters=schema({"limit": INT, "kind": STR}),
            handler=memory_recent,
            tags=["memory", "read"],
            cacheable=True,
        ),
    ]


def build_task_tools(ctx: ToolContext) -> list[HarnessTool]:
    async def task_list() -> dict:
        plan = ctx.plan_provider() if ctx.plan_provider else None
        if plan is None:
            return {"tasks": [], "count": 0}
        return {
            "objective": plan.objective,
            "strategy": plan.strategy,
            "tasks": [
                {
                    "id": t.id,
                    "seq": t.seq,
                    "title": t.title,
                    "agent": t.agent,
                    "status": t.status,
                    "depends_on": t.depends_on,
                    "attempts": t.attempts,
                    "summary": t.result_summary,
                    "artifacts": t.artifacts,
                }
                for t in plan.tasks
            ],
            "count": len(plan.tasks),
        }

    async def task_get(task_id: str) -> dict:
        plan = ctx.plan_provider() if ctx.plan_provider else None
        task = plan.by_id(task_id) if plan else None
        if task is None:
            return {"error": f"task not found: {task_id}"}
        return task.model_dump(mode="json")

    return [
        HarnessTool(
            name="task_list",
            description="查看当前运行的任务清单及状态（pending/in_progress/completed/failed 等）。",
            parameters=schema({}),
            handler=task_list,
            tags=["task", "read"],
            cacheable=False,
        ),
        HarnessTool(
            name="task_get",
            description="查看单个任务的详情、依赖与产出文件。",
            parameters=schema({"task_id": STR}, ["task_id"]),
            handler=task_get,
            tags=["task", "read"],
            cacheable=False,
        ),
    ]


def build_builtin_tools(ctx: ToolContext) -> list[HarnessTool]:
    return [
        *build_fs_tools(ctx),
        *build_skill_tools(ctx),
        *build_sandbox_tools(ctx),
        *build_memory_tools(ctx),
        *build_task_tools(ctx),
    ]
