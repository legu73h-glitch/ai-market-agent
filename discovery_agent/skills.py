"""Загрузка скиллов и разбор их front-matter.

Каждый скилл — это папка с `SKILL.md`. Из YAML-front-matter берём
`name`, `inputs`, `outputs` и `metadata.tools`; тело (markdown после
front-matter) используем как системный промпт ноды.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# Полный discovery-конвейер (порядок нод из README/WORKFLOW).
PIPELINE_SKILLS = [
    "brief-writing",
    "market-research",
    "persona-generation",
    "lean-canvas",
    "user-story-mapping",
    "wireframe-spec",
    "persona-interview",
]

# Минимальный стартовый пайплайн из WORKFLOW.md.
MINIMAL_SKILLS = ["brief-writing", "user-story-mapping", "wireframe-spec"]

_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class Skill:
    """Один скилл-нода конвейера."""

    name: str
    title: str
    body: str          # системный промпт (markdown без front-matter)
    inputs: list[str]
    outputs: list[str]
    tools: list[str]
    path: Path


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Делит документ на YAML-front-matter и тело."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm_raw = text[3:end].strip("\n")
            body = text[end + 4:].lstrip("\n")
            meta = yaml.safe_load(fm_raw) or {}
            return meta, body
    return {}, text


def _first_heading(body: str, fallback: str) -> str:
    m = _HEADING_RE.search(body)
    return m.group(1).strip() if m else fallback


def load_skill(skill_dir: Path) -> Skill:
    """Читает `<skill_dir>/SKILL.md` и возвращает объект Skill."""
    md = skill_dir / "SKILL.md"
    if not md.exists():
        raise FileNotFoundError(f"Не найден SKILL.md в {skill_dir}")

    meta, body = _split_frontmatter(md.read_text(encoding="utf-8"))
    metadata = meta.get("metadata") or {}
    name = str(meta.get("name") or skill_dir.name)

    return Skill(
        name=name,
        title=_first_heading(body, name),
        body=body,
        inputs=[str(x) for x in (meta.get("inputs") or [])],
        outputs=[str(x) for x in (meta.get("outputs") or [])],
        tools=[str(x) for x in (metadata.get("tools") or [])],
        path=md,
    )


def load_skills(skills_dir: Path, names: list[str]) -> dict[str, Skill]:
    """Загружает набор скиллов по именам папок, сохраняя порядок."""
    out: dict[str, Skill] = {}
    for folder in names:
        skill = load_skill(skills_dir / folder)
        if skill.name in out:
            raise ValueError(f"Дублирующееся имя скилла: {skill.name!r}")
        out[skill.name] = skill
    return out
