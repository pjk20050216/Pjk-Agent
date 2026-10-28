"""Skills 技能系统 —— 渐进式披露（Progressive Disclosure）。

三级加载：
1. **元数据**：启动时只扫描 ``SKILL.md`` 的 YAML frontmatter（name/description/triggers），
   注入主 Agent 的技能目录，几乎不占 token；
2. **正文**：Agent 判断需要时调用 ``skill_view``，才把完整 SOP / 规则模板读入上下文；
3. **资源**：脚本、SQL 模板、领域文档按需通过 ``skill_resource`` 读取。

这样既能让 Agent “知道自己会什么”，又不会一次性把所有领域文档塞进上下文。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from harness.config import get_harness_config
from server.utils.logger import logger


class SkillMeta(BaseModel):
    name: str
    description: str = ""
    version: str = "1.0.0"
    triggers: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    path: str = ""
    resources: list[str] = Field(default_factory=list)
    body_chars: int = 0


class Skill:
    def __init__(self, meta: SkillMeta, body: str, directory: Path) -> None:
        self.meta = meta
        self.body = body
        self.directory = directory

    def resource(self, rel_path: str) -> str:
        target = (self.directory / rel_path).resolve()
        if not target.is_relative_to(self.directory.resolve()):
            raise PermissionError(f"resource escapes skill directory: {rel_path}")
        if not target.is_file():
            raise FileNotFoundError(rel_path)
        return target.read_text(encoding="utf-8", errors="replace")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, parts[2].lstrip("\n")


class SkillRegistry:
    def __init__(self, skills_dir: Path | None = None) -> None:
        self.skills_dir = Path(skills_dir or get_harness_config().skills_dir)
        self._skills: dict[str, Skill] = {}
        self.load_all()

    # -------------------------------------------------------------------- load
    def load_all(self) -> int:
        self._skills.clear()
        if not self.skills_dir.exists():
            logger.warning(f"[Skills] directory not found: {self.skills_dir}")
            return 0
        for skill_file in sorted(self.skills_dir.glob("*/SKILL.md")):
            try:
                text = skill_file.read_text(encoding="utf-8")
                raw, body = _parse_frontmatter(text)
                name = str(raw.get("name") or skill_file.parent.name)
                resources = sorted(
                    str(p.relative_to(skill_file.parent)).replace("\\", "/")
                    for p in skill_file.parent.rglob("*")
                    if p.is_file() and p.name != "SKILL.md"
                )
                meta = SkillMeta(
                    name=name,
                    description=str(raw.get("description", "")).strip(),
                    version=str(raw.get("version", "1.0.0")),
                    triggers=[str(t) for t in (raw.get("triggers") or [])],
                    tags=[str(t) for t in (raw.get("tags") or [])],
                    tools=[str(t) for t in (raw.get("tools") or [])],
                    path=str(skill_file.parent),
                    resources=resources,
                    body_chars=len(body),
                )
                self._skills[name] = Skill(meta, body, skill_file.parent)
            except Exception as exc:
                logger.warning(f"[Skills] failed to load {skill_file}: {exc}")
        logger.info(f"[Skills] loaded {len(self._skills)} skills from {self.skills_dir}")
        return len(self._skills)

    # ------------------------------------------------------------------- query
    def list_meta(self) -> list[SkillMeta]:
        return [skill.meta for skill in self._skills.values()]

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def load_body(self, name: str) -> str:
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"unknown skill: {name}")
        return skill.body

    def load_resource(self, name: str, rel_path: str) -> str:
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"unknown skill: {name}")
        return skill.resource(rel_path)

    def match(self, query: str, limit: int = 5) -> list[SkillMeta]:
        """按触发词 / 标签 / 名称匹配技能（确定性、零 token）。"""
        q = (query or "").lower()
        scored: list[tuple[float, SkillMeta]] = []
        for skill in self._skills.values():
            meta = skill.meta
            score = 0.0
            for trigger in meta.triggers:
                if trigger and trigger.lower() in q:
                    score += 3.0
            for tag in meta.tags:
                if tag and tag.lower() in q:
                    score += 1.0
            if meta.name.lower() in q:
                score += 2.0
            for token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", meta.description):
                if token.lower() in q:
                    score += 0.3
            if score > 0:
                scored.append((score, meta))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [meta for _, meta in scored[:limit]]

    def catalog_prompt(self, max_chars: int = 2400) -> str:
        """只含名称与一句话描述的技能目录（渐进式披露第一级）。"""
        lines = []
        for skill in self._skills.values():
            meta = skill.meta
            triggers = f"（触发：{'/'.join(meta.triggers[:4])}）" if meta.triggers else ""
            lines.append(f"- {meta.name}: {meta.description}{triggers}")
        text = "\n".join(lines)
        return text[:max_chars]

    def __len__(self) -> int:
        return len(self._skills)


skills = SkillRegistry()
