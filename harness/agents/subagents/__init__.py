"""子 Agent 注册表。"""
from __future__ import annotations

from harness.agents.base import BaseAgent
from harness.agents.subagents.data_collection import DataCollectionAgent
from harness.agents.subagents.specialists import (
    AnomalyDiagnosisAgent,
    FeatureAnalysisAgent,
    KnowledgeRetrievalAgent,
    RuleConfigAgent,
    TestValidationAgent,
)

_AGENTS: dict[str, BaseAgent] = {
    agent.name: agent
    for agent in (
        FeatureAnalysisAgent(),
        RuleConfigAgent(),
        TestValidationAgent(),
        AnomalyDiagnosisAgent(),
        KnowledgeRetrievalAgent(),
        DataCollectionAgent(),
    )
}

AGENT_CATALOG = [
    {"name": agent.name, "description": agent.description}
    for agent in _AGENTS.values()
]


def get_agent(name: str) -> BaseAgent | None:
    return _AGENTS.get(name)


def list_agents() -> list[dict]:
    return list(AGENT_CATALOG)
