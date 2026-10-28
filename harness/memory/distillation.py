"""经验蒸馏 —— 任务结束后复盘，沉淀新的排查模式与提示模板。

优先让 LLM 归纳；模型不可用或输出不合法时，用确定性的启发式规则生成，
保证「自我进化」能力在离线环境也可演示。
"""
from __future__ import annotations

import re

from harness.contracts import MemoryRecord, RunSnapshot, SubAgentResult, TaskStatus
from harness.llm import complete_json, llm_available, truncate
from server.utils.logger import logger

DOMAIN_KEYWORDS = [
    "规则", "策略", "灰度", "回测", "阈值", "特征", "名单", "额度", "交易",
    "设备", "关联", "团伙", "误杀", "漏放", "申诉", "审批", "风控", "反欺诈",
    "支付", "营销", "评分卡", "模型", "变量", "命中", "拦截", "放行",
]

ALLOWED_KINDS = {"experience", "template", "preference", "case"}


def extract_tags(text: str, extra: list[str] | None = None) -> list[str]:
    tags = [kw for kw in DOMAIN_KEYWORDS if kw in text]
    for item in extra or []:
        if item and item not in tags:
            tags.append(item)
    # 去重并限制长度
    return tags[:8]


def _heuristic_records(
    snapshot: RunSnapshot,
    results: list[SubAgentResult],
) -> list[MemoryRecord]:
    records: list[MemoryRecord] = []
    plan = snapshot.plan
    if plan is None:
        return records

    for result in results:
        if not result.success or not result.summary:
            continue
        task = plan.by_id(result.task_id)
        title = task.title if task else result.agent
        text = f"排查模式「{title}」：{truncate(result.summary, 500)}"
        if result.findings:
            text += "\n关键发现：" + "；".join(result.findings[:4])
        records.append(
            MemoryRecord(
                kind="experience",
                text=text,
                tags=extract_tags(text, [result.agent]),
                run_id=snapshot.id,
                user_id=snapshot.user_id,
                metadata={"task_id": result.task_id, "source": "heuristic"},
            )
        )

    successful = [r for r in results if r.success]
    if successful:
        steps = [
            f"{i + 1}. {plan.by_id(r.task_id).title if plan.by_id(r.task_id) else r.agent}"
            for i, r in enumerate(successful)
        ]
        text = (
            f"可复用排查 SOP：目标「{snapshot.objective}」。\n"
            + "\n".join(steps)
            + "\n适用场景："
            + "、".join(extract_tags(snapshot.objective) or ["风控策略运维"])
        )
        records.append(
            MemoryRecord(
                kind="template",
                text=text,
                tags=extract_tags(snapshot.objective, ["SOP"]),
                run_id=snapshot.id,
                user_id=snapshot.user_id,
                metadata={"source": "heuristic", "strategy": plan.strategy},
            )
        )

    failed = [r for r in results if not r.success]
    if failed:
        detail = "；".join(
            f"{r.agent}: {truncate(r.error or r.summary or '未知失败', 160)}" for r in failed[:3]
        )
        records.append(
            MemoryRecord(
                kind="case",
                text=f"失败复盘：目标「{snapshot.objective}」执行受阻。{detail}",
                tags=extract_tags(snapshot.objective, ["失败复盘"]),
                run_id=snapshot.id,
                user_id=snapshot.user_id,
                metadata={"source": "heuristic"},
            )
        )
    return records[:5]


async def _llm_records(
    snapshot: RunSnapshot,
    results: list[SubAgentResult],
) -> list[MemoryRecord]:
    if not llm_available():
        return []
    plan = snapshot.plan
    lines = []
    for result in results:
        task = plan.by_id(result.task_id) if plan else None
        lines.append(
            f"- 任务：{task.title if task else result.agent} | 成功={result.success} | "
            f"结论：{truncate(result.summary, 300)} | 发现：{'；'.join(result.findings[:3])}"
        )
    prompt = (
        "你是风控策略运维专家。请对一次 Agent 运行做复盘，提炼可复用的排查模式、"
        "提示模板或经验教训。输出 JSON 数组，最多 3 条，每条形如：\n"
        '{"kind":"experience|template|preference|case","text":"...","tags":["..."]}\n'
        "要求：具体、可操作、避免空话；text 不超过 300 字。\n\n"
        f"目标：{snapshot.objective}\n"
        f"编排策略：{plan.strategy if plan else ''}\n"
        f"任务结果：\n" + "\n".join(lines)
    )
    data = await complete_json(
        [
            {"role": "system", "content": "你是严谨的风控策略复盘专家，只输出合法 JSON。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=1200,
    )
    if not isinstance(data, list):
        return []
    records: list[MemoryRecord] = []
    for item in data[:3]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        kind = str(item.get("kind", "experience"))
        if kind not in ALLOWED_KINDS:
            kind = "experience"
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        records.append(
            MemoryRecord(
                kind=kind,
                text=truncate(text, 600),
                tags=[str(t)[:20] for t in tags][:8],
                run_id=snapshot.id,
                user_id=snapshot.user_id,
                metadata={"source": "llm", "model": "active"},
            )
        )
    return records


async def distill_run(
    snapshot: RunSnapshot,
    results: list[SubAgentResult],
) -> list[MemoryRecord]:
    """复盘一次运行，返回待写入长期记忆的条目。"""
    if not results:
        return []
    records: list[MemoryRecord] = []
    try:
        records = await _llm_records(snapshot, results)
    except Exception as exc:
        logger.warning(f"[Distill] LLM distillation failed: {exc}")
    if not records:
        records = _heuristic_records(snapshot, results)
    # 过滤掉完全失败且无信息的运行
    if not any(r.success for r in results) and snapshot.plan:
        if all(
            task.status in {TaskStatus.FAILED.value, TaskStatus.BLOCKED.value}
            for task in snapshot.plan.tasks
        ):
            return records[:1]
    return records
