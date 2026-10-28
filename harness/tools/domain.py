"""风控域工具 —— 特征、规则、回测、日志、知识与审批。

工具只负责“做事”，是否放行由中间件链决定（权限 → 审批 → 审计）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import yaml

from harness.contracts import RiskLevel
from harness.tools import risk_data
from harness.tools.base import BOOL, INT, NUM, STR, HarnessTool, schema
from harness.tools.context import ToolContext


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_domain_tools(ctx: ToolContext) -> list[HarnessTool]:
    vfs = ctx.vfs

    # ------------------------------------------------------------- features
    async def feature_query(feature: str, segment: str = "all", window: str = "7d") -> dict:
        data = risk_data.query_feature(feature, segment=segment, window=window)
        if data is None:
            return {
                "error": f"unknown feature: {feature}",
                "available": sorted(risk_data.FEATURES),
            }
        return data

    async def feature_stats(features: list[str]) -> dict:
        rows = []
        for feature in features:
            meta = risk_data.feature_stats(feature)
            if meta is None:
                rows.append({"feature": feature, "error": "not registered"})
            else:
                rows.append(meta)
        return {"features": rows, "count": len(rows)}

    # ----------------------------------------------------------------- rules
    async def rule_list() -> dict:
        return {"rules": risk_data.list_rules()}

    async def rule_get(rule_id: str, version: int | None = None) -> dict:
        data = risk_data.get_rule(rule_id, version)
        if data is None:
            return {"error": f"rule not found: {rule_id} v{version or 'active'}"}
        return data

    async def rule_diff(rule_id: str, from_version: int, to_version: int) -> dict:
        diff = risk_data.diff_rule(rule_id, from_version, to_version)
        path = f"/workspace/rule_diff_{rule_id}_{_now_tag()}.md"
        await vfs.write(
            path,
            f"# 规则差异 {rule_id}\n\n```diff\n{diff}\n```\n",
            author="agent",
            comment="rule diff",
        )
        return {"rule_id": rule_id, "from_version": from_version, "to_version": to_version, "diff": diff, "artifact": path}

    async def rule_validate(dsl: str) -> dict:
        result = risk_data.validate_rule(dsl)
        return result

    async def rule_apply(rule_id: str, dsl: str, comment: str = "") -> dict:
        validation = risk_data.validate_rule(dsl)
        if not validation["valid"]:
            return {"success": False, "validation": validation}
        try:
            data = yaml.safe_load(dsl)
        except yaml.YAMLError as exc:
            return {"success": False, "error": f"YAML 解析失败: {exc}"}
        version = int(data.get("version", 1))
        path = f"/policies/{rule_id}/v{version}.yaml"
        entry = await vfs.write(path, dsl, author="agent", comment=comment or "rule draft")
        draft_path = f"/policies/{rule_id}/draft.yaml"
        await vfs.write(draft_path, dsl, author="agent", comment="latest draft")
        return {
            "success": True,
            "rule_id": rule_id,
            "version": version,
            "path": path,
            "draft_path": draft_path,
            "file_version": entry.version,
            "validation": validation,
            "note": "已保存草稿，尚未发布；发布需人工审批。",
        }

    async def rule_publish(rule_id: str, version: int, comment: str = "") -> dict:
        rule = risk_data.get_rule(rule_id, version)
        if rule is None:
            return {"error": f"rule not found: {rule_id} v{version}"}
        ts = _now_tag()
        record = {
            "rule_id": rule_id,
            "version": version,
            "action": "publish",
            "comment": comment,
            "at": _iso(),
            "operator": "agent",
            "run_id": ctx.run_id,
        }
        record_path = f"/policies/{rule_id}/publish_v{version}_{ts}.json"
        await vfs.write(
            record_path,
            yaml.safe_dump(record, allow_unicode=True, sort_keys=False),
            author="agent",
            comment="publish record",
        )
        approval_material = _approval_material(rule, "发布", comment, ctx.run_id)
        approval_path = f"/reports/approval_{rule_id}_v{version}_{ts}.md"
        await vfs.write(approval_path, approval_material, author="agent", comment="approval material")
        return {
            "success": True,
            "rule_id": rule_id,
            "version": version,
            "status": "published",
            "record": record_path,
            "approval_material": approval_path,
            "note": "模拟发布完成；生产环境此处调用 Java 风控平台发布接口。",
        }

    async def rule_gray_release(rule_id: str, percent: int, comment: str = "") -> dict:
        if not 0 <= percent <= 100:
            return {"error": "percent must be 0-100"}
        rule = risk_data.get_rule(rule_id)
        if rule is None:
            return {"error": f"rule not found: {rule_id}"}
        ts = _now_tag()
        record = {
            "rule_id": rule_id,
            "version": rule["version"],
            "percent": percent,
            "action": "gray_release",
            "comment": comment,
            "at": _iso(),
            "run_id": ctx.run_id,
        }
        path = f"/policies/{rule_id}/gray_{ts}.json"
        await vfs.write(
            path,
            yaml.safe_dump(record, allow_unicode=True, sort_keys=False),
            author="agent",
            comment="gray release record",
        )
        return {
            "success": True,
            "rule_id": rule_id,
            "percent": percent,
            "record": path,
            "observation": "建议观察期不少于 24 小时，监控误杀率/客诉量/P99 耗时",
        }

    async def rule_rollback(rule_id: str, target_version: int, reason: str) -> dict:
        rule = risk_data.get_rule(rule_id, target_version)
        if rule is None:
            return {"error": f"target version not found: {rule_id} v{target_version}"}
        ts = _now_tag()
        path = f"/policies/{rule_id}/rollback_{ts}.json"
        await vfs.write(
            path,
            yaml.safe_dump(
                {
                    "rule_id": rule_id,
                    "target_version": target_version,
                    "reason": reason,
                    "at": _iso(),
                    "run_id": ctx.run_id,
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            author="agent",
            comment="rollback record",
        )
        return {"success": True, "rule_id": rule_id, "target_version": target_version, "record": path}

    # ------------------------------------------------------------------ logs
    async def rule_backtest(
        rule_id: str, sample_size: int = 2000, version: int | None = None
    ) -> dict:
        result = risk_data.backtest(rule_id, sample_size=sample_size, version=version)
        if "error" in result:
            return result
        ts = _now_tag()
        path = f"/reports/backtest_{rule_id}_v{result['test_version']}_{ts}.md"
        metrics = result["metrics"]
        report = "\n".join(
            [
                f"# 回测报告 {rule_id}",
                "",
                f"- 基线版本：v{result['base_version']}",
                f"- 测试版本：v{result['test_version']}",
                f"- 样本量：{result['sample_size']}（{result['sample_window']}）",
                f"- 置信度：{result['confidence']}",
                "",
                "## 指标对比",
                "| 指标 | 变更前 | 变更后 | 变化 |",
                "|---|---|---|---|",
                f"| 命中量 | {metrics['hit_count']['before']} | {metrics['hit_count']['after']} | {metrics['hit_count']['delta']:+d} |",
                f"| 误杀量 | {metrics['false_positive']['before']} | {metrics['false_positive']['after']} | {metrics['false_positive']['delta']:+d} |",
                f"| 漏放量 | {metrics['false_negative']['before']} | {metrics['false_negative']['after']} | {metrics['false_negative']['delta']:+d} |",
                f"| 命中率 | {metrics['hit_rate']['before']:.2%} | {metrics['hit_rate']['after']:.2%} | - |",
                f"| 误杀率 | {metrics['false_positive_rate']['before']:.2%} | {metrics['false_positive_rate']['after']:.2%} | - |",
                "",
                "## 结论",
                result["conclusion"],
                "",
                "## 差异样本（抽样）",
                "```json",
                yaml.safe_dump(result["diff_samples"], allow_unicode=True, sort_keys=False),
                "```",
            ]
        )
        await vfs.write(path, report, author="agent", comment="backtest report")
        result["report_path"] = path
        return result

    async def log_search(query: str, time_window: str = "1h", limit: int = 50) -> dict:
        lines = risk_data.search_logs(query, time_window=time_window, limit=limit)
        path = f"/logs/log_snapshot_{_now_tag()}.log"
        body = "\n".join(item["line"] for item in lines) or "(no match)"
        await vfs.write(path, body, author="agent", comment=f"log search: {query}")
        return {
            "query": query,
            "time_window": time_window,
            "count": len(lines),
            "snapshot": path,
            "lines": lines[:20],
            "truncated": len(lines) > 20,
        }

    # -------------------------------------------------------------- knowledge
    async def knowledge_search(query: str, top_k: int = 5) -> dict:
        memories = await ctx.memory.recall(query, user_id=ctx.user_id, top_k=top_k)
        matched_skills = ctx.skills.match(query, limit=3)
        return {
            "query": query,
            "memories": [m.model_dump(mode="json") for m in memories],
            "skills": [s.model_dump(mode="json") for s in matched_skills],
            "hint": "如需技能正文，请调用 skill_view；技能资源用 skill_resource。",
        }

    # --------------------------------------------------------------- approval
    async def approval_request(action: str, target: str, reason: str) -> dict:
        approval = await ctx.approvals.request(
            run_id=ctx.run_id,
            action=action,
            target=target,
            risk=RiskLevel.HIGH,
            reason=reason,
            payload={"requested_via": "approval_request_tool"},
        )
        decided = await ctx.approvals.wait(approval.id)
        return {
            "approval_id": decided.id,
            "status": str(decided.status),
            "decided_by": decided.decided_by,
            "comment": decided.comment,
        }

    return [
        HarnessTool(
            name="feature_query",
            description="查询单个特征的统计信息（均值、分位、空值率、PSI、异常提示）。",
            parameters=schema(
                {"feature": STR, "segment": STR, "window": STR},
                ["feature"],
            ),
            handler=feature_query,
            tags=["feature", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="feature_stats",
            description="批量查询多个特征的统计信息。",
            parameters=schema(
                {"features": {"type": "array", "items": STR}},
                ["features"],
            ),
            handler=feature_stats,
            tags=["feature", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="rule_list",
            description="列出所有风控规则及其当前版本、灰度比例、负责人。",
            parameters=schema({}),
            handler=rule_list,
            tags=["rule", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="rule_get",
            description="获取指定规则的 DSL、版本、灰度状态与依赖特征。",
            parameters=schema({"rule_id": STR, "version": INT}, ["rule_id"]),
            handler=rule_get,
            tags=["rule", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="rule_diff",
            description="对比规则两个版本的 DSL 差异，并把 diff 写入 /workspace。",
            parameters=schema(
                {"rule_id": STR, "from_version": INT, "to_version": INT},
                ["rule_id", "from_version", "to_version"],
            ),
            handler=rule_diff,
            tags=["rule", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="rule_validate",
            description="校验规则 DSL 的结构、特征存在性、操作符与灰度比例。",
            parameters=schema({"dsl": STR}, ["dsl"]),
            handler=rule_validate,
            tags=["rule", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="rule_apply",
            description="保存规则草稿到 /policies（不发布），自动校验并留痕。",
            parameters=schema(
                {"rule_id": STR, "dsl": STR, "comment": STR},
                ["rule_id", "dsl"],
            ),
            handler=rule_apply,
            risk=RiskLevel.MEDIUM,
            action="policy.draft",
            tags=["rule", "write"],
        ),
        HarnessTool(
            name="rule_publish",
            description="发布规则版本（高风险，必须人工审批），生成发布记录与审批材料。",
            parameters=schema(
                {"rule_id": STR, "version": INT, "comment": STR},
                ["rule_id", "version"],
            ),
            handler=rule_publish,
            risk=RiskLevel.HIGH,
            action="rule.publish",
            tags=["rule", "write", "risky"],
            requires_approval=True,
        ),
        HarnessTool(
            name="rule_gray_release",
            description="调整规则灰度比例（高风险，必须人工审批）。",
            parameters=schema(
                {"rule_id": STR, "percent": INT, "comment": STR},
                ["rule_id", "percent"],
            ),
            handler=rule_gray_release,
            risk=RiskLevel.HIGH,
            action="rule.gray_release",
            tags=["rule", "write", "risky"],
            requires_approval=True,
        ),
        HarnessTool(
            name="rule_rollback",
            description="回滚规则到指定版本（高风险，必须人工审批）。",
            parameters=schema(
                {"rule_id": STR, "target_version": INT, "reason": STR},
                ["rule_id", "target_version", "reason"],
            ),
            handler=rule_rollback,
            risk=RiskLevel.HIGH,
            action="rule.publish",
            tags=["rule", "write", "risky"],
            requires_approval=True,
        ),
        HarnessTool(
            name="rule_backtest",
            description="在历史样本上回测规则版本，返回命中/误杀/漏放/金额影响与结论。",
            parameters=schema(
                {"rule_id": STR, "sample_size": INT, "version": INT},
                ["rule_id"],
            ),
            handler=rule_backtest,
            tags=["test", "read"],
            cacheable=False,
        ),
        HarnessTool(
            name="log_search",
            description="按关键字检索决策日志，返回命中行并把日志快照写入 /logs。",
            parameters=schema(
                {"query": STR, "time_window": STR, "limit": INT},
                ["query"],
            ),
            handler=log_search,
            tags=["log", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="knowledge_search",
            description="同时检索长期经验记忆与技能库，返回相关经验与技能元数据。",
            parameters=schema({"query": STR, "top_k": INT}, ["query"]),
            handler=knowledge_search,
            tags=["knowledge", "read"],
            cacheable=True,
        ),
        HarnessTool(
            name="approval_request",
            description="主动发起一次人工审批并等待结果（用于需要显式审批的业务动作）。",
            parameters=schema(
                {"action": STR, "target": STR, "reason": STR},
                ["action", "target", "reason"],
            ),
            handler=approval_request,
            risk=RiskLevel.LOW,
            action="approval.request",
            tags=["approval"],
        ),
    ]


def _approval_material(rule: dict, action: str, comment: str, run_id: str) -> str:
    metrics = risk_data.backtest(rule["rule_id"], sample_size=2000, version=rule["version"])
    lines = [
        f"# 规则{action}审批材料",
        "",
        f"- 规则：{rule['name']}（{rule['rule_id']}）",
        f"- 版本：v{rule['version']}",
        f"- 负责人：{rule['owner']}",
        f"- 运行 ID：{run_id}",
        f"- 生成时间：{_iso()}",
        f"- 变更说明：{comment or '（未填写）'}",
        "",
        "## 变更内容",
        "```yaml",
        rule["dsl"].strip(),
        "```",
        "",
        "## 回测结果",
        f"- 样本量：{metrics['sample_size']}",
        f"- 命中量：{metrics['metrics']['hit_count']['before']} → {metrics['metrics']['hit_count']['after']}",
        f"- 误杀量：{metrics['metrics']['false_positive']['before']} → {metrics['metrics']['false_positive']['after']}",
        f"- 漏放量：{metrics['metrics']['false_negative']['before']} → {metrics['metrics']['false_negative']['after']}",
        f"- 结论：{metrics['conclusion']}",
        "",
        "## 灰度与回滚",
        "- 灰度计划：1% → 5% → 20% → 100%，每阶段观察不少于 24 小时",
        "- 回滚版本：v1",
        "- 熔断条件：误杀率上升 > 0.5pp、客诉量环比 > 2 倍、P99 上升 > 30%",
        "",
        "## 审批意见",
        "- 审批人：",
        "- 结论：",
        "- 备注：",
    ]
    return "\n".join(lines)
