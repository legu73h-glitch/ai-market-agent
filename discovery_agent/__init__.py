"""AI Product Manager — discovery-конвейер.

Из одной идеи продукта собирает пакет discovery-артефактов, прогоняя
семь скиллов-нод через Anthropic Messages API. Граф зависимостей между
нодами выводится автоматически из полей `inputs`/`outputs` в front-matter
каждого SKILL.md — порядок исполнения повторяет WORKFLOW.md.
"""

from .config import Config
from .skills import Skill, load_skills, PIPELINE_SKILLS, MINIMAL_SKILLS
from .pipeline import build_plan, run_pipeline, PipelinePlan, PipelineResult
from .client import NodeResult

__all__ = [
    "Config",
    "Skill",
    "load_skills",
    "PIPELINE_SKILLS",
    "MINIMAL_SKILLS",
    "build_plan",
    "run_pipeline",
    "PipelinePlan",
    "PipelineResult",
    "NodeResult",
]
