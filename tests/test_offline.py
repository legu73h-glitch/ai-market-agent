"""Оффлайн-проверка конвейера с замоканным клиентом Anthropic.

Гоняет полный граф без сети и проверяет:
  • порядок волн (DAG соответствует WORKFLOW.md);
  • параллельный запуск и передачу входов между нодами;
  • обработку pause_turn и подсчёт веб-поисков в ноде market-research;
  • форму запроса (model / effort / adaptive thinking / tools);
  • сохранение артефактов и сборку сводного пакета.

Запуск:  python -m unittest tests.test_offline   (сеть и ключ не нужны)
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from discovery_agent import build_plan, load_skills, run_pipeline
from discovery_agent.artifacts import build_package, save_artifacts
from discovery_agent.config import Config
from discovery_agent.skills import PIPELINE_SKILLS

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def _block(type_: str, **kw):
    return NS(type=type_, **kw)


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _FakeMessages:
    """Имитация client.messages: web_search-нода делает pause_turn, затем end_turn."""

    def __init__(self, calls: list):
        self.calls = calls

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        messages = kwargs["messages"]
        has_tools = "tools" in kwargs
        is_continuation = messages[-1]["role"] == "assistant"

        if has_tools and not is_continuation:
            # Первый заход ноды с web_search: сервер «поискал» и приостановился.
            content = [
                _block("server_tool_use", name="web_search", id="srvtu_1", input={"query": "x"}),
                _block("text", text="Ищу источники…"),
            ]
            return _FakeStream(NS(content=content, usage=NS(input_tokens=120, output_tokens=15),
                                  stop_reason="pause_turn"))

        text = "# Готовый артефакт\n\nСодержимое, собранное нодой."
        content = [_block("text", text=text)]
        return _FakeStream(NS(content=content, usage=NS(input_tokens=300, output_tokens=80),
                              stop_reason="end_turn"))


class _FakeClient:
    def __init__(self):
        self.calls: list = []
        self.messages = _FakeMessages(self.calls)


class PipelineOfflineTest(unittest.TestCase):
    def setUp(self):
        self.skills = load_skills(SKILLS_DIR, PIPELINE_SKILLS)
        self.cfg = Config()

    def test_plan_matches_workflow(self):
        plan = build_plan(self.skills)
        levels = [set(level) for level in plan.levels]
        self.assertEqual(levels[0], {"brief-writing"})
        self.assertEqual(levels[1], {"market-research", "persona-generation"})
        self.assertEqual(levels[2], {"lean-canvas", "persona-interview", "user-story-mapping"})
        self.assertEqual(levels[3], {"wireframe-spec"})

    def test_full_run_offline(self):
        fake = _FakeClient()
        with mock.patch("discovery_agent.pipeline.make_client", return_value=fake):
            result = run_pipeline(self.cfg, self.skills, "Идея тестового продукта")

        # Все семь артефактов собраны, провалов нет.
        self.assertTrue(result.ok, msg=f"failed={result.failed} skipped={result.skipped}")
        for artifact in ("brief", "market", "personas", "lean_canvas",
                         "story_map", "wireframes", "interview_report"):
            self.assertIn(artifact, result.artifacts)
            self.assertTrue(result.artifacts[artifact].strip())

        # market-research: один веб-поиск, завершился корректно (не обрыв).
        market = result.results["market-research"]
        self.assertEqual(market.web_searches, 1)
        self.assertEqual(market.stop_reason, "end_turn")
        self.assertFalse(market.truncated)
        # Токены накоплены по обоим заходам (pause + продолжение).
        self.assertEqual(market.input_tokens, 120 + 300)

        # Нода без инструментов — без веб-поиска.
        self.assertEqual(result.results["brief-writing"].web_searches, 0)

        # Форма запроса: модель, усилие, adaptive thinking; tools только у market.
        tool_calls = [c for c in fake.calls if "tools" in c]
        plain_calls = [c for c in fake.calls if "tools" not in c]
        self.assertTrue(tool_calls, "ожидался хотя бы один вызов с web_search")
        for call in fake.calls:
            self.assertEqual(call["model"], self.cfg.model)
            self.assertEqual(call["output_config"], {"effort": self.cfg.effort})
            self.assertEqual(call["thinking"], {"type": "adaptive"})
        for call in tool_calls:
            self.assertEqual(call["tools"][0]["type"], "web_search_20260209")
        self.assertTrue(plain_calls)

    def test_artifacts_written(self):
        fake = _FakeClient()
        with mock.patch("discovery_agent.pipeline.make_client", return_value=fake):
            result = run_pipeline(self.cfg, self.skills, "Идея")
        with tempfile.TemporaryDirectory() as tmp:
            written = save_artifacts(Path(tmp), result.artifacts)
            names = {p.name for p in written}
            self.assertIn("brief.md", names)
            self.assertIn("wireframes.md", names)
            self.assertIn("discovery-package.md", names)
            package = build_package(result.artifacts)
            self.assertIn("# Discovery-пакет", package)
            self.assertIn("## Бриф", package)
            self.assertIn("## Вайрфреймы", package)


if __name__ == "__main__":
    unittest.main(verbosity=2)
