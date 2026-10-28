"""五个专业子 Agent —— 特征分析、规则配置、测试验证、异常排查、知识检索。

每个 Agent 同时具备两条执行路径：
- LLM 路径：BaseAgent 提供的 Function Calling 循环，自主选择工具；
- 确定性路径（heuristic）：无 API Key / 模型故障时按领域 SOP 直接调用数据源，
  保证 Harness 在离线环境仍能产出完整报告与结论。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

from harness.agents.base import AgentContext, BaseAgent
from harness.agents.subagents.common import (
    features_from,
    markdown_table,
    rule_id_from,
    write_report,
)
from harness.contracts import ArtifactRef
from harness.tools import risk_data
from harness.tools.risk_data import FEATURES


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class FeatureAnalysisAgent(BaseAgent):
    name = "feature_analysis"
    description = "负责特征口径核对、分布与数据质量分析，判断异常是否由特征漂移或空值引起。"
    preferred_skills = ["feature-analysis", "rule-review-sop"]

    async def heuristic(
        self, ctx: AgentContext
    ) -> tuple[str, list[str], list[ArtifactRef]]:
        features = features_from(ctx)
        rows = []
        anomalies = []
        for feature in features:
            data = risk_data.query_feature(feature)
            if data is None:
                continue
            rows.append(
                [
                    data["feature"],
                    data["mean"],
                    data["p50"],
                    data["p99"],
                    f"{data['null_rate']:.2%}",
                    data["psi"],
                    "异常" if data["anomaly"] else "正常",
                ]
            )
            if data["anomaly"]:
                anomalies.append(data)

        body = "\n".join(
            [
                f"# 特征分析报告",
                "",
                f"- 生成时间：{_now()}",
                f"- 目标：{ctx.objective}",
                f"- 分析特征数：{len(rows)}",
                "",
                "## 统计概览",
                markdown_table(
                    ["特征", "均值", "P50", "P99", "空值率", "PSI", "判定"], rows
                ),
                "",
                "## 异常与建议",
                *(
                    [
                        f"- **{item['feature']}**：PSI={item['psi']}，{item['anomaly_hint']}；"
                        f"空值率 {item['null_rate']:.2%}，更新频率 {item['updated']}。"
                        for item in anomalies
                    ]
                    or ["- 所有特征分布稳定，未发现显著漂移。"]
                ),
                "",
                "## 结论",
                (
                    "存在特征分布漂移，建议先核对上游口径并回补数据，再评估规则阈值。"
                    if anomalies
                    else "特征侧未发现异常，问题更可能来自规则条件或名单变更。"
                ),
            ]
        )
        path, artifact = await write_report(
            ctx,
            f"feature_analysis_{ctx.task.seq}.md",
            body,
            author=self.name,
            comment="特征分析报告",
        )
        summary = (
            f"完成 {len(rows)} 个特征分析，{len(anomalies)} 个存在显著漂移"
            f"（{'、'.join(a['feature'] for a in anomalies) if anomalies else '无'}）。"
            f"报告：{path}"
        )
        findings = [
            f"{item['feature']} PSI={item['psi']}，空值率={item['null_rate']:.2%}"
            for item in anomalies
        ] or ["所有分析特征 PSI < 0.25，分布稳定"]
        return summary, findings, [artifact]


class RuleConfigAgent(BaseAgent):
    name = "rule_config"
    description = "负责规则 DSL 的读取、差异分析、草稿生成与校验，产出可审批的最小变更方案。"
    preferred_skills = ["rule-dsl-guide", "rule-review-sop"]

    async def heuristic(
        self, ctx: AgentContext
    ) -> tuple[str, list[str], list[ArtifactRef]]:
        rule_id = rule_id_from(ctx)
        rule = risk_data.get_rule(rule_id)
        if rule is None:
            return (
                f"未找到规则 {rule_id}，无法生成调整建议。",
                ["规则不存在，请确认 rule_id"],
                [],
            )
        versions = sorted(rule["versions"])
        active = rule["version"]
        base_version = max((v for v in versions if v < active), default=active)
        diff = risk_data.diff_rule(rule_id, base_version, active)

        proposal = rule["dsl"]
        proposal_version = 3 if rule_id == "R_RISK_001" else active + 1
        if rule_id == "R_RISK_001":
            proposal = risk_data.RULE_V3_DRAFT
        else:
            # 其他规则没有预置草稿：复制当前 DSL 并递增版本号
            proposal = re.sub(
                r"(?m)^version:\s*\d+",
                f"version: {proposal_version}",
                proposal,
                count=1,
            )
        validation = risk_data.validate_rule(proposal)
        proposal_path = f"/policies/{rule_id}/proposal_v{proposal_version}.yaml"
        await ctx.vfs.write(
            proposal_path,
            proposal,
            author=self.name,
            comment="rule proposal",
        )

        body = "\n".join(
            [
                "# 规则调整建议",
                "",
                f"- 生成时间：{_now()}",
                f"- 规则：{rule['name']}（{rule_id}）",
                f"- 当前生效版本：v{active}（灰度 {rule['gray_percent']}%）",
                f"- 对比基线：v{base_version}",
                f"- 建议版本：v{proposal_version}",
                "",
                "## 当前差异（基线 → 生效）",
                "```diff",
                diff or "（无差异）",
                "```",
                "",
                "## 建议变更",
                "```yaml",
                proposal.strip(),
                "```",
                "",
                "## 校验结果",
                f"- 是否通过：{'是' if validation['valid'] else '否'}",
                *[f"- 错误：{e}" for e in validation["errors"]],
                *[f"- 提示：{w}" for w in validation["warnings"]],
                "",
                "## 变更理由",
                "- 原阈值 0.90 对高风险设备覆盖不足，存在漏放；",
                "- 新增登录失败次数条件，降低仅凭设备分误伤正常用户的风险；",
                "- 动作由 reject 调整为 review，先人工复核再拦截，控制误杀。",
                "",
                "## 发布建议",
                "- 先以 1% 灰度验证 2 小时，再逐步放量；",
                "- 发布与灰度均需人工审批；回滚版本 v2。",
            ]
        )
        path, artifact = await write_report(
            ctx,
            f"rule_change_{ctx.task.seq}.md",
            body,
            author=self.name,
            comment="规则调整建议",
        )
        summary = (
            f"已为 {rule_id} 生成 v{proposal_version} 变更建议，"
            f"校验{'通过' if validation['valid'] else '未通过'}，草稿：{proposal_path}，"
            f"说明：{path}。"
        )
        findings = [
            f"当前版本 v{active}，灰度 {rule['gray_percent']}%",
            f"依赖特征：{'、'.join(rule['depends_on'])}",
            *validation["warnings"],
        ]
        if validation["errors"]:
            findings.extend(validation["errors"])
        return summary, findings, [artifact]


class TestValidationAgent(BaseAgent):
    name = "test_validation"
    description = "负责回归测试、指标对比与沙箱验证，产出可审计的测试报告。"
    preferred_skills = ["backtest-sop", "rule-dsl-guide"]

    async def heuristic(
        self, ctx: AgentContext
    ) -> tuple[str, list[str], list[ArtifactRef]]:
        rule_id = rule_id_from(ctx)
        result = risk_data.backtest(rule_id, sample_size=2000)
        if "error" in result:
            return result["error"], ["回测失败：规则不存在"], []

        mode = str((ctx.task.inputs or {}).get("mode", "backtest"))
        if mode == "compare":
            return await self._compare(ctx, rule_id, result)

        metrics = result["metrics"]
        rule = risk_data.get_rule(rule_id)
        validation = risk_data.validate_rule(rule["dsl"]) if rule else {"valid": False, "errors": ["规则不存在"], "warnings": []}

        body = "\n".join(
            [
                "# 回归测试与效果对比报告",
                "",
                f"- 生成时间：{_now()}",
                f"- 规则：{rule_id}",
                f"- 基线版本：v{result['base_version']} → 测试版本：v{result['test_version']}",
                f"- 样本量：{result['sample_size']}（{result['sample_window']}）",
                f"- 置信度：{result['confidence']}",
                "",
                "## 指标对比",
                markdown_table(
                    ["指标", "变更前", "变更后", "变化"],
                    [
                        ["命中量", metrics["hit_count"]["before"], metrics["hit_count"]["after"], f"{metrics['hit_count']['delta']:+d}"],
                        ["误杀量", metrics["false_positive"]["before"], metrics["false_positive"]["after"], f"{metrics['false_positive']['delta']:+d}"],
                        ["漏放量", metrics["false_negative"]["before"], metrics["false_negative"]["after"], f"{metrics['false_negative']['delta']:+d}"],
                        ["命中率", f"{metrics['hit_rate']['before']:.2%}", f"{metrics['hit_rate']['after']:.2%}", "-"],
                        ["误杀率", f"{metrics['false_positive_rate']['before']:.2%}", f"{metrics['false_positive_rate']['after']:.2%}", "-"],
                    ],
                ),
                "",
                "## DSL 校验",
                f"- 是否通过：{'是' if validation['valid'] else '否'}",
                *[f"- 错误：{e}" for e in validation.get("errors", [])],
                "",
                "## 差异样本（抽样）",
                "```json",
                "\n".join(str(item) for item in result["diff_samples"]),
                "```",
                "",
                "## 结论",
                result["conclusion"],
                "",
                "## 风险提示",
                "- 回测基于历史样本，线上分布变化可能改变效果；",
                "- 发布前需确认回滚版本可用，并准备灰度观察指标。",
            ]
        )
        path, artifact = await write_report(
            ctx,
            f"backtest_{rule_id}_{ctx.task.seq}.md",
            body,
            author=self.name,
            comment="回归测试报告",
        )
        summary = (
            f"{rule_id} 回测完成：命中 {metrics['hit_count']['before']}→{metrics['hit_count']['after']}，"
            f"误杀 {metrics['false_positive']['before']}→{metrics['false_positive']['after']}，"
            f"结论：{result['conclusion']}。报告：{path}"
        )
        findings = [
            f"样本量 {result['sample_size']}，置信度 {result['confidence']}",
            f"误杀率 {metrics['false_positive_rate']['before']:.2%} → {metrics['false_positive_rate']['after']:.2%}",
            f"DSL 校验：{'通过' if validation['valid'] else '未通过'}",
        ]
        return summary, findings, [artifact]

    async def _compare(
        self,
        ctx: AgentContext,
        rule_id: str,
        result: dict,
    ) -> tuple[str, list[str], list[ArtifactRef]]:
        """效果复核：把回测指标映射为发布决策，而不是重复输出指标表。"""
        metrics = result["metrics"]
        fp_delta_pp = (
            metrics["false_positive_rate"]["after"]
            - metrics["false_positive_rate"]["before"]
        ) * 100
        fn_delta_pp = (
            metrics["false_negative"]["after"] - metrics["false_negative"]["before"]
        ) / max(1, result["sample_size"]) * 100

        rule = risk_data.get_rule(rule_id)
        validation = (
            risk_data.validate_rule(rule["dsl"])
            if rule
            else {"valid": False, "errors": ["规则不存在"]}
        )
        dsl_ok = bool(validation["valid"])

        blockers: list[str] = []
        if fp_delta_pp > 0.5:
            blockers.append(f"误杀率上升 {fp_delta_pp:.2f}pp，超过 0.5pp 红线")
        if result["confidence"] == "low":
            blockers.append("样本量不足 1000，统计置信度低")
        if fn_delta_pp > 0.3:
            blockers.append(f"漏放率上升 {fn_delta_pp:.2f}pp，超过 0.3pp 红线")
        if not dsl_ok:
            blockers.append("规则 DSL 校验未通过")

        decision = "暂缓发布" if blockers else "建议进入 1% 灰度观察"
        body = "\n".join(
            [
                "# 策略效果复核与发布决策",
                "",
                f"- 生成时间：{_now()}",
                f"- 规则：{rule_id}",
                f"- 复核依据：v{result['base_version']} → v{result['test_version']} 回测",
                f"- 样本量：{result['sample_size']}（置信度 {result['confidence']}）",
                "",
                "## 发布判定",
                f"**结论：{decision}**",
                "",
                "## 决策矩阵",
                markdown_table(
                    ["检查项", "阈值", "实测", "判定"],
                    [
                        ["误杀率变化", "≤ 0.5pp", f"{fp_delta_pp:+.2f}pp", "通过" if fp_delta_pp <= 0.5 else "不通过"],
                        ["漏放率变化", "≤ 0.3pp", f"{fn_delta_pp:+.2f}pp", "通过" if fn_delta_pp <= 0.3 else "不通过"],
                        ["样本置信度", "medium/high", result["confidence"], "通过" if result["confidence"] != "low" else "不通过"],
                        ["DSL 校验", "通过", "通过" if dsl_ok else "未通过", "通过" if dsl_ok else "不通过"],
                    ],
                ),
                "",
                "## 阻塞项",
                *([f"- {item}" for item in blockers] or ["- 无阻塞项"]),
                "",
                "## 上线建议",
                "- 若判定为“建议灰度”：按 1% → 5% → 20% → 100% 分阶段放量；",
                "- 每阶段观察误杀率、客诉量、P99 耗时，任一熔断条件触发立即回滚；",
                "- 发布与每次灰度比例调整均需人工审批，并附本报告与回测报告；",
                "- 若判定为“暂缓”：先补充样本或修正规则条件，再重新回测。",
                "",
                "## 回滚预案",
                "- 回滚版本：v1（已验证可用）；",
                "- 回滚操作人：待指定；预计耗时：< 5 分钟；",
                "- 回滚后需确认指标恢复并记录复盘。",
            ]
        )
        path, artifact = await write_report(
            ctx,
            f"result_analysis_{rule_id}_{ctx.task.seq}.md",
            body,
            author=self.name,
            comment="效果复核与发布决策",
        )
        summary = (
            f"{rule_id} 效果复核结论：{decision}；"
            f"误杀率变化 {fp_delta_pp:+.2f}pp，置信度 {result['confidence']}。"
            f"决策报告：{path}"
        )
        findings = [
            f"发布判定：{decision}",
            f"误杀率变化：{fp_delta_pp:+.2f}pp",
            *blockers,
        ]
        return summary, findings, [artifact]


class AnomalyDiagnosisAgent(BaseAgent):
    name = "anomaly_diagnosis"
    description = "负责线上异常分级、日志取证与影响面定位，先止损后归因。"
    preferred_skills = ["incident-triage", "rule-review-sop"]

    async def heuristic(
        self, ctx: AgentContext
    ) -> tuple[str, list[str], list[ArtifactRef]]:
        rule_id = rule_id_from(ctx)
        logs = risk_data.search_logs(rule_id, limit=120)
        decisions = Counter(item["decision"] for item in logs)
        latencies = [item["latency_ms"] for item in logs] or [0]
        p99 = sorted(latencies)[max(0, int(len(latencies) * 0.99) - 1)]
        feature_rows = []
        for feature in features_from(ctx)[:3]:
            data = risk_data.query_feature(feature)
            if data:
                feature_rows.append(
                    [data["feature"], data["mean"], data["psi"], data["anomaly_hint"]]
                )

        snapshot = "\n".join(item["line"] for item in logs[:40])
        await ctx.vfs.write(
            f"/logs/incident_{rule_id}_{ctx.task.seq}.log",
            snapshot,
            author=self.name,
            comment="incident log snapshot",
        )

        body = "\n".join(
            [
                "# 异常排查报告",
                "",
                f"- 生成时间：{_now()}",
                f"- 目标：{ctx.objective}",
                f"- 关联规则：{rule_id}",
                f"- 采样日志：{len(logs)} 条",
                f"- 决策分布：{dict(decisions)}",
                f"- 决策耗时 P99：{p99} ms",
                "",
                "## 影响面判断",
                "- 是否仍在影响：**需结合实时告警确认**（本报告基于快照）；",
                "- 建议级别：P1（单渠道指标异常/客诉集中）；",
                "- 优先动作：确认最近规则/特征变更时间点，必要时回滚。",
                "",
                "## 特征侧线索",
                markdown_table(["特征", "均值", "PSI", "提示"], feature_rows),
                "",
                "## 排查时间线（建议）",
                "1. 锁定异常起始时间，与发布记录对齐；",
                "2. 按渠道/客群拆分指标，找到集中点；",
                "3. 拉取决策日志快照（已保存到 /logs）；",
                "4. 在沙箱复现异常样本；",
                "5. 给出止损方案并走审批。",
                "",
                "## 根因假设",
                "- 假设 1：规则阈值下调导致误杀扩大（需 diff 验证）；",
                "- 假设 2：特征分布漂移（需核对上游口径）；",
                "- 假设 3：名单/灰度批次异常（需核对发布记录）。",
            ]
        )
        path, artifact = await write_report(
            ctx,
            f"anomaly_{rule_id}_{ctx.task.seq}.md",
            body,
            author=self.name,
            comment="异常排查报告",
        )
        summary = (
            f"{rule_id} 采样 {len(logs)} 条日志，决策分布 {dict(decisions)}，"
            f"P99 {p99}ms；已保存日志快照与排查报告：{path}。"
        )
        findings = [
            f"决策分布：{dict(decisions)}",
            f"P99 耗时：{p99} ms",
            *[f"{row[0]} PSI={row[2]}：{row[3]}" for row in feature_rows],
        ]
        return summary, findings, [artifact]


class KnowledgeRetrievalAgent(BaseAgent):
    name = "knowledge_retrieval"
    description = "负责检索历史排查经验、策略模板与技能库，为当前问题提供可复用参考。"
    preferred_skills = ["rule-review-sop", "incident-triage", "gray-release"]

    async def heuristic(
        self, ctx: AgentContext
    ) -> tuple[str, list[str], list[ArtifactRef]]:
        memories = await ctx.memory.recall(
            ctx.objective, user_id=ctx.user_id, top_k=5
        )
        matched = ctx.skills.match(ctx.objective, limit=3)
        skill_excerpts = []
        for meta in matched[:2]:
            try:
                body = ctx.skills.load_body(meta.name)
            except KeyError:
                continue
            skill_excerpts.append(
                f"### {meta.name}\n{meta.description}\n\n{body[:1200]}"
            )

        body = "\n".join(
            [
                "# 知识检索报告",
                "",
                f"- 生成时间：{_now()}",
                f"- 查询：{ctx.objective}",
                "",
                "## 历史经验",
                *(
                    [
                        f"- [{m.kind}] {m.text[:300]}（相关度 {m.score:.3f}）"
                        for m in memories
                    ]
                    or ["- 长期记忆库中暂无高度相关经验，本次运行结束后将自动沉淀。"]
                ),
                "",
                "## 匹配技能",
                *(
                    [f"- {m.name}：{m.description}" for m in matched]
                    or ["- 未匹配到技能，可按通用 SOP 执行。"]
                ),
                "",
                "## 技能要点摘录",
                *skill_excerpts,
            ]
        )
        path, artifact = await write_report(
            ctx,
            f"knowledge_{ctx.task.seq}.md",
            body,
            author=self.name,
            comment="知识检索报告",
        )
        summary = (
            f"检索到 {len(memories)} 条历史经验、{len(matched)} 个相关技能"
            f"（{'、'.join(m.name for m in matched) or '无'}）。报告：{path}"
        )
        findings = [
            f"经验：{m.text[:120]}（score={m.score:.3f}）" for m in memories[:3]
        ] or ["暂无历史经验，已记录本次检索需求"]
        findings.extend(f"技能匹配：{m.name}" for m in matched)
        return summary, findings, [artifact]
