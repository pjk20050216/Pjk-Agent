"""主 Agent 编排器 —— 任务规划与最终结果分析。

主 Agent 不直接读大日志/大报告：它只持有任务清单、子 Agent 的结构化结论与
VFS 文件引用，因此上下文随任务数线性增长，而不是随原始数据量增长。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from harness.agents.subagents import AGENT_CATALOG
from harness.config import HarnessConfig, get_harness_config
from harness.contracts import (
    MemoryRecord,
    RiskLevel,
    SubAgentResult,
    TaskItem,
    TaskPlan,
    TaskStatus,
)
from harness.llm import complete, complete_json, llm_available, truncate
from harness.vfs.store import VirtualFileSystem
from server.utils.logger import logger

INCIDENT_KEYWORDS = ["误杀", "漏放", "异常", "告警", "客诉", "突增", "失效", "线上"]
RULE_KEYWORDS = ["规则", "阈值", "调整", "发布", "灰度", "回滚", "名单", "策略变更"]
FEATURE_KEYWORDS = ["特征", "变量", "分布", "漂移", "空值", "分箱", "psi"]
DATA_KEYWORDS = [
    "抓取", "采集", "爬取", "搜集", "收集", "检索公开", "搜索公开",
    "网页", "公开数据", "舆情", "新闻", "公告", "外部数据", "数据源", "资料",
]


class Orchestrator:
    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config or get_harness_config()

    # ------------------------------------------------------------------- plan
    async def plan(
        self,
        objective: str,
        memory_context: list[MemoryRecord] | None = None,
    ) -> TaskPlan:
        memory_context = memory_context or []
        if llm_available():
            plan = await self._llm_plan(objective, memory_context)
            if plan and plan.tasks:
                return plan
        return self.heuristic_plan(objective)

    async def _llm_plan(
        self,
        objective: str,
        memory_context: list[MemoryRecord],
    ) -> TaskPlan | None:
        agents_text = "\n".join(
            f"- {item['name']}: {item['description']}" for item in AGENT_CATALOG
        )
        memory_text = "\n".join(
            f"- [{m.kind}] {truncate(m.text, 200)}" for m in memory_context[:5]
        ) or "（无）"
        prompt = (
            "你是风控策略运维 Harness 的主 Agent。请把用户目标拆解为可执行任务清单。\n\n"
            f"用户目标：{objective}\n\n"
            f"可用子 Agent：\n{agents_text}\n\n"
            f"历史经验（参考，不要照搬）：\n{memory_text}\n\n"
            "输出 JSON：\n"
            '{"strategy":"编排策略一句话","tasks":['
            '{"title":"任务标题","description":"具体做什么","agent":"子Agent名称",'
            '"depends_on":[依赖的任务序号，从1开始],"inputs":{},"acceptance":["验收标准"],'
            '"risk":"low|medium|high"}]}\n\n'
            "要求：\n"
            f"- 任务数 3~{self.config.max_tasks}，按执行顺序编号；\n"
            "- 只有确实需要前序产出时才设置 depends_on，能并行的不要强行串行；\n"
            "- 覆盖：问题定位 → 特征/案例 → 规则调整 → 回归测试 → 效果对比 → 结论；\n"
            "- 涉及外部网页、新闻、公告、公开数据采集时，必须使用 data_collection 子 Agent；\n"
            "- 不要创建“写最终报告”任务，主 Agent 会汇总；\n"
            "- 只输出 JSON。"
        )
        data = await complete_json(
            [
                {"role": "system", "content": "你是严谨的风控策略任务规划器，只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=1800,
        )
        if not isinstance(data, dict):
            return None
        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return None

        valid_agents = {item["name"] for item in AGENT_CATALOG}
        tasks: list[TaskItem] = []
        seq_to_id: dict[int, str] = {}
        seq_to_task: dict[int, TaskItem] = {}
        for index, raw in enumerate(raw_tasks[: self.config.max_tasks], start=1):
            if not isinstance(raw, dict):
                continue
            agent = str(raw.get("agent", "")).strip()
            if agent not in valid_agents:
                agent = "feature_analysis"
            task = TaskItem(
                seq=index,
                title=str(raw.get("title") or f"任务 {index}")[:120],
                description=str(raw.get("description", ""))[:1000],
                agent=agent,
                inputs=raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {},
                acceptance=[
                    str(a)[:200]
                    for a in (raw.get("acceptance") or [])
                    if isinstance(a, (str, int, float))
                ][:6],
                risk=_risk(raw.get("risk")),
                priority=100 - index,
            )
            seq_to_id[index] = task.id
            seq_to_task[index] = task
            tasks.append(task)

        for index, raw in enumerate(raw_tasks[: self.config.max_tasks], start=1):
            task = seq_to_task.get(index)
            if task is None or not isinstance(raw, dict):
                continue
            deps = raw.get("depends_on")
            if isinstance(deps, list):
                task.depends_on = [
                    seq_to_id[int(dep)]
                    for dep in deps
                    if str(dep).isdigit() and int(dep) in seq_to_id and int(dep) < index
                ]

        if not tasks:
            return None
        return TaskPlan(
            objective=objective,
            strategy=str(data.get("strategy", ""))[:500],
            tasks=tasks,
            metadata={"planner": "llm"},
        )

    def heuristic_plan(self, objective: str) -> TaskPlan:
        text = objective.lower()
        incident = any(k in objective for k in INCIDENT_KEYWORDS)
        rule_change = any(k in objective for k in RULE_KEYWORDS)
        feature_only = any(k in objective for k in FEATURE_KEYWORDS)
        data_requested = any(k in objective for k in DATA_KEYWORDS)
        data_only = data_requested and not (incident or rule_change or feature_only)

        specs: list[dict] = []
        if data_requested:
            specs.append(
                {
                    "title": "外部数据采集",
                    "description": (
                        "检索公开来源并抓取网页正文，抽取结构化记录，"
                        "原始快照与数据集落 VFS，输出采集报告。"
                    ),
                    "agent": "data_collection",
                    "inputs": {"queries": [objective], "max_sources": 5},
                    "acceptance": [
                        "给出数据集路径",
                        "记录来源 URL 与抓取时间",
                        "说明失败与合规限制",
                    ],
                    "risk": "medium",
                }
            )
        if incident:
            specs.append(
                {
                    "title": "异常定位与影响面分析",
                    "description": "拉取决策日志、判断异常级别与影响面，形成止损建议。",
                    "agent": "anomaly_diagnosis",
                    "inputs": {},
                    "acceptance": ["给出是否仍在影响", "给出止损方案", "日志快照已归档"],
                    "risk": "low",
                }
            )
            specs.append(
                {
                    "title": "特征与数据质量核查",
                    "description": "核对规则依赖特征的分布、空值率与 PSI，排除特征漂移。",
                    "agent": "feature_analysis",
                    "inputs": {},
                    "acceptance": ["覆盖全部依赖特征", "给出异常特征结论"],
                    "risk": "low",
                }
            )
        elif feature_only:
            specs.append(
                {
                    "title": "特征分布与数据质量分析",
                    "description": "分析特征口径、空值、分位与分布漂移。",
                    "agent": "feature_analysis",
                    "inputs": {},
                    "acceptance": ["给出 PSI 与空值率", "给出是否漂移的结论"],
                    "risk": "low",
                }
            )
        else:
            specs.append(
                {
                    "title": "风险问题分析",
                    "description": "结合目标描述与规则现状，明确要解决的问题与影响范围。",
                    "agent": "anomaly_diagnosis",
                    "inputs": {},
                    "acceptance": ["明确问题定义", "给出影响范围假设"],
                    "risk": "low",
                }
            )
            if rule_change:
                specs.append(
                    {
                        "title": "规则依赖特征核查",
                        "description": "核对规则依赖特征的分布、空值率与 PSI，为阈值调整提供依据。",
                        "agent": "feature_analysis",
                        "inputs": {},
                        "acceptance": ["覆盖全部依赖特征", "给出异常特征结论"],
                        "risk": "low",
                    }
                )

        if not feature_only:
            specs.append(
                {
                    "title": "历史案例与经验检索",
                    "description": "检索长期记忆与技能库，复用历史排查模式与模板。",
                    "agent": "knowledge_retrieval",
                    "inputs": {},
                    "acceptance": ["返回相关经验与技能", "给出可复用要点"],
                    "risk": "low",
                }
            )

        if rule_change or incident or not feature_only:
            specs.append(
                {
                    "title": "规则调整建议",
                    "description": "读取规则 DSL，分析差异并生成最小变更草稿，完成结构校验。",
                    "agent": "rule_config",
                    "inputs": {},
                    "acceptance": ["生成规则草稿", "校验通过", "给出灰度与回滚建议"],
                    "risk": "medium",
                }
            )
            specs.append(
                {
                    "title": "回归测试与效果对比",
                    "description": "在历史样本上回测变更前后版本，对比命中、误杀、漏放与金额影响。",
                    "agent": "test_validation",
                    "inputs": {"mode": "backtest"},
                    "acceptance": ["样本量说明", "指标对比表", "明确结论"],
                    "risk": "low",
                }
            )
            specs.append(
                {
                    "title": "策略效果复核与结果分析",
                    "description": "复核回测结论，评估发布风险并形成可审批的最终建议。",
                    "agent": "test_validation",
                    "inputs": {"mode": "compare"},
                    "acceptance": ["复核误杀/漏放变化", "给出发布或暂缓建议"],
                    "risk": "low",
                }
            )

        if data_only:
            # 纯数据采集：只保留数据 Agent，避免误触发风控规则变更链路
            specs = [spec for spec in specs if spec["agent"] == "data_collection"]

        tasks: list[TaskItem] = []
        anomaly_id: str | None = None
        feature_id: str | None = None
        rule_task_id: str | None = None
        test_task_id: str | None = None
        for index, spec in enumerate(specs[: self.config.max_tasks], start=1):
            task = TaskItem(
                seq=index,
                title=spec["title"],
                description=spec["description"],
                agent=spec["agent"],
                inputs=spec.get("inputs", {}),
                acceptance=spec.get("acceptance", []),
                risk=RiskLevel(spec.get("risk", "low")),
                priority=100 - index,
            )
            # 依赖关系：异常定位 → 特征核查 → 规则调整 → 回归测试 → 效果复核；
            # 知识检索保持独立，可与前序任务并行。
            if task.agent == "feature_analysis" and anomaly_id:
                task.depends_on = [anomaly_id]
            elif task.agent == "rule_config" and feature_id:
                task.depends_on = [feature_id]
            elif task.agent == "test_validation":
                if test_task_id:
                    task.depends_on = [test_task_id]
                elif rule_task_id:
                    task.depends_on = [rule_task_id]
            tasks.append(task)
            if task.agent == "anomaly_diagnosis":
                anomaly_id = task.id
            elif task.agent == "feature_analysis":
                feature_id = task.id
            elif task.agent == "rule_config":
                rule_task_id = task.id
            elif task.agent == "test_validation":
                test_task_id = task.id

        if data_only:
            strategy = (
                "委派数据采集 Agent 检索并抓取公开数据，原始内容与结构化数据集落 VFS，"
                "主 Agent 汇总来源、数据规模与限制。"
            )
        elif data_requested:
            strategy = (
                "并行采集外部数据并执行风控排查链路；数据证据与规则结论分别落 VFS，"
                "主 Agent 汇总为可审批材料。"
            )
        elif not feature_only:
            strategy = (
                "先并行定位异常与检索历史经验，再基于特征结论生成规则变更，"
                "最后串行执行回归测试与效果复核，主 Agent 汇总审批材料。"
            )
        else:
            strategy = "聚焦特征侧分析，结合历史经验给出结论。"
        return TaskPlan(
            objective=objective,
            strategy=strategy,
            tasks=tasks,
            metadata={"planner": "heuristic"},
        )

    # -------------------------------------------------------------- synthesis
    async def synthesize(
        self,
        *,
        run_id: str,
        objective: str,
        plan: TaskPlan,
        results: list[SubAgentResult],
        vfs: VirtualFileSystem,
    ) -> tuple[str, str]:
        # 按任务序号排序，避免并发完成顺序打乱报告结构
        results = sorted(
            results,
            key=lambda r: (
                plan.by_id(r.task_id).seq if plan.by_id(r.task_id) else 999
            ),
        )
        artifacts = []
        for result in results:
            artifacts.extend(a.path for a in result.artifacts)
        content = None
        if llm_available():
            content = await self._llm_synthesis(objective, plan, results, artifacts)
        if not content:
            content = self._deterministic_synthesis(run_id, objective, plan, results, artifacts)

        path = f"/reports/final_report_{run_id}.md"
        await vfs.write(path, content, author="main_agent", comment="final report")
        return content, path

    async def _llm_synthesis(
        self,
        objective: str,
        plan: TaskPlan,
        results: list[SubAgentResult],
        artifacts: list[str],
    ) -> str | None:
        lines = []
        for result in results:
            lines.append(
                f"- 任务 {result.task_id}（{result.agent}）：success={result.success}；"
                f"结论：{truncate(result.summary, 500)}；发现：{'；'.join(result.findings[:4])}"
            )
        prompt = (
            "你是风控策略负责人。请基于子 Agent 结论，生成一份可提交审批的最终分析报告。\n"
            "必须包含：1) 结论摘要；2) 任务执行情况；3) 关键证据；4) 规则变更建议；"
            "5) 风险与回滚方案；6) 待人工确认事项。\n"
            "要求：区分已证实/推测/待验证；引用产出文件路径；不要编造未给出的指标。\n\n"
            f"目标：{objective}\n编排策略：{plan.strategy}\n\n"
            f"子 Agent 结论：\n" + "\n".join(lines) + "\n\n"
            f"产出文件：\n" + "\n".join(f"- {p}" for p in artifacts)
        )
        return await complete(
            [
                {"role": "system", "content": "你是严谨的风控策略负责人，输出结构化 Markdown。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=3000,
        )

    def _deterministic_synthesis(
        self,
        run_id: str,
        objective: str,
        plan: TaskPlan,
        results: list[SubAgentResult],
        artifacts: list[str],
    ) -> str:
        completed = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        lines = [
            "# 风控策略运行分析报告",
            "",
            f"- 运行 ID：{run_id}",
            f"- 生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"- 目标：{objective}",
            f"- 编排策略：{plan.strategy}",
            f"- 任务完成：{len(completed)}/{len(results)}",
            "",
            "## 一、结论摘要",
            "",
        ]
        for result in completed:
            lines.append(f"- {result.summary}")
        if failed:
            lines.append("")
            lines.append("### 未完成任务")
            for result in failed:
                lines.append(f"- {result.agent}：{result.error or result.summary}")
        if not completed:
            lines.append("- 本次运行未产出有效结论，请检查外部依赖或调整任务输入。")

        lines.extend(
            [
                "",
                "## 二、任务执行情况",
                "",
                "| # | 任务 | Agent | 状态 | 耗时(ms) | 结论 |",
                "|---|---|---|---|---|---|",
            ]
        )
        for index, result in enumerate(results, start=1):
            task = plan.by_id(result.task_id)
            title = task.title if task else result.task_id
            lines.append(
                f"| {index} | {title} | {result.agent} | "
                f"{'完成' if result.success else '失败'} | {result.duration_ms or 0} | "
                f"{truncate(result.summary, 120)} |"
            )

        lines.extend(["", "## 三、关键证据", ""])
        for result in completed:
            if result.findings:
                lines.append(f"### {result.agent}")
                lines.extend(f"- {finding}" for finding in result.findings[:6])
                lines.append("")

        lines.extend(["## 四、产出资产", ""])
        unique = list(dict.fromkeys(artifacts))
        lines.extend(f"- `{path}`" for path in unique) if unique else lines.append("- 无")

        lines.extend(
            [
                "",
                "## 五、风险与回滚",
                "- 任何规则发布 / 灰度调整均需人工审批，审批单见运行详情；",
                "- 建议灰度路径：1% → 5% → 20% → 100%，每阶段观察不少于 24 小时；",
                "- 回滚触发条件：误杀率上升 > 0.5pp、客诉量环比 > 2 倍、P99 上升 > 30%；",
                "- 回滚前确认历史版本可用，并记录操作人与时间。",
                "",
                "## 六、待人工确认",
                "- 回测样本是否覆盖本次异常客群；",
                "- 特征口径近期是否发生变更；",
                "- 灰度范围与观察指标是否符合业务预期；",
                "- 审批材料是否包含变更 diff、验证证据与回滚方案。",
                "",
                "> 说明：本报告由 Harness 自动生成。标记为「推测/待验证」的结论需人工复核，"
                "不得直接作为全量发布依据。",
            ]
        )
        return "\n".join(lines)


def _risk(value: object) -> RiskLevel:
    try:
        return RiskLevel(str(value))
    except ValueError:
        return RiskLevel.LOW
