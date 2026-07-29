"""Построение графа зависимостей и исполнение конвейера.

Зависимости выводятся из полей `inputs`/`outputs` скиллов: нода зависит
от тех нод, что производят её входные артефакты (кроме внешнего `idea`).
Независимые ноды исполняются параллельно в пуле потоков.
"""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable

from .client import NodeResult, make_client, run_node
from .skills import Skill

# Внешний вход конвейера — идея продукта от пользователя.
ROOT_ARTIFACT = "idea"


@dataclass
class PipelinePlan:
    """План исполнения: уровни параллелизма, зависимости, производители."""

    levels: list[list[str]]           # ноды по «волнам» для параллельного запуска
    deps: dict[str, set[str]]         # нода -> ноды-предшественники
    producers: dict[str, str]         # артефакт -> производящая нода


def build_plan(skills: dict[str, Skill]) -> PipelinePlan:
    """Строит и валидирует граф зависимостей между нодами."""
    producers: dict[str, str] = {}
    for skill in skills.values():
        for artifact in skill.outputs:
            if artifact in producers:
                raise ValueError(
                    f"Артефакт {artifact!r} производят две ноды: "
                    f"{producers[artifact]!r} и {skill.name!r}"
                )
            producers[artifact] = skill.name

    # Вход считается «доступным», если это idea или его производит одна из
    # включённых нод. Прочие объявленные входы — необязательные обогащения:
    # нода отработает без них (автономный режим скилла закроет пробел
    # допущениями). Жёсткое требование — хотя бы один доступный вход, иначе
    # нода оторвана от конвейера.
    deps: dict[str, set[str]] = {}
    for skill in skills.values():
        node_deps: set[str] = set()
        available: list[str] = []
        for artifact in skill.inputs:
            if artifact == ROOT_ARTIFACT:
                available.append(artifact)
                continue
            producer = producers.get(artifact)
            if producer is not None:
                node_deps.add(producer)
                available.append(artifact)
        if not available:
            wanted = ", ".join(skill.inputs) or "—"
            raise ValueError(
                f"Ноде {skill.name!r} нужны входы [{wanted}], но в этом наборе "
                f"их никто не производит и среди них нет {ROOT_ARTIFACT!r}. "
                f"Добавьте ноды-производители или уберите {skill.name!r}."
            )
        deps[skill.name] = node_deps

    # Разложение по уровням (заодно ловим циклы).
    levels: list[list[str]] = []
    resolved: set[str] = set()
    remaining = set(skills)
    while remaining:
        level = sorted(n for n in remaining if deps[n] <= resolved)
        if not level:
            raise ValueError(
                f"Цикл в графе зависимостей среди нод: {sorted(remaining)}"
            )
        levels.append(level)
        resolved |= set(level)
        remaining -= set(level)

    return PipelinePlan(levels=levels, deps=deps, producers=producers)


@dataclass
class PipelineResult:
    """Итог прогона конвейера."""

    artifacts: dict[str, str]              # имя артефакта -> его содержимое
    results: dict[str, NodeResult]         # имя ноды -> результат
    failed: dict[str, str]                 # имя ноды -> текст ошибки
    skipped: list[str]                     # ноды, пропущенные из-за упавших зависимостей
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed and not self.skipped


def run_pipeline(
    config,
    skills: dict[str, Skill],
    idea: str,
    on_event: Callable[..., None] = lambda *a, **k: None,
) -> PipelineResult:
    """Исполняет конвейер, соблюдая зависимости и параллеля независимые ноды.

    on_event(kind, node, **info) вызывается из главного потока со стадиями:
    "start", "done" (result=...), "error" (error=...), "skipped".
    """
    plan = build_plan(skills)
    deps = plan.deps

    artifacts: dict[str, str] = {ROOT_ARTIFACT: idea}
    results: dict[str, NodeResult] = {}
    failed: dict[str, str] = {}
    done: set[str] = set()
    started: set[str] = set()

    client = make_client(config)
    start_t = time.monotonic()

    def ready() -> list[str]:
        out = []
        for name in skills:
            if name in started or name in failed:
                continue
            node_deps = deps[name]
            if node_deps & set(failed):
                continue  # недостижима — предшественник упал
            if node_deps <= done:
                out.append(name)
        return sorted(out)

    with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        futures: dict = {}

        def launch() -> None:
            for name in ready():
                started.add(name)
                # Передаём только доступные входы; необязательные отсутствующие
                # артефакты просто не попадают в промпт ноды.
                node_inputs = {
                    inp: artifacts[inp] for inp in skills[name].inputs if inp in artifacts
                }
                future = executor.submit(run_node, client, config, skills[name], node_inputs)
                futures[future] = name
                on_event("start", name)

        launch()
        while futures:
            done_futs, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for future in done_futs:
                name = futures.pop(future)
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 — фиксируем и продолжаем
                    failed[name] = f"{type(exc).__name__}: {exc}"
                    on_event("error", name, error=failed[name])
                    continue
                results[name] = result
                for artifact in skills[name].outputs:
                    artifacts[artifact] = result.text
                done.add(name)
                on_event("done", name, result=result)
            launch()

    skipped = sorted(n for n in skills if n not in done and n not in failed)
    for name in skipped:
        on_event("skipped", name)

    return PipelineResult(
        artifacts=artifacts,
        results=results,
        failed=failed,
        skipped=skipped,
        elapsed=time.monotonic() - start_t,
    )
