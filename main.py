#!/usr/bin/env python3
"""AI-агент продакта — discovery-конвейер.

Из одной идеи продукта собирает пакет discovery-артефактов:
бриф → (рынок ∥ персоны) → интервью → (Lean Canvas, story map) → вайрфреймы.

Примеры:
    python main.py --idea "Сервис подписки на здоровые обеды для офисов"
    python main.py --idea-file idea.txt --output out/ --model claude-sonnet-5
    python main.py --minimal --idea "..."           # бриф → story map → вайрфреймы
    python main.py --idea "..." --dry-run           # показать план без вызовов API
    python main.py --list-skills                    # список доступных скиллов
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from discovery_agent.artifacts import ARTIFACT_TITLES, save_artifacts
from discovery_agent.config import DEFAULT_MODEL, VALID_EFFORTS, Config
from discovery_agent.pipeline import build_plan, run_pipeline
from discovery_agent.skills import (
    MINIMAL_SKILLS,
    PIPELINE_SKILLS,
    load_skills,
)

# Цены Anthropic ($/1M токенов: вход, выход) для грубой оценки стоимости.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}


def _parse_args(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="discovery",
        description="AI-агент продакта: из идеи — пакет discovery-артефактов.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--idea", help="Текст идеи продукта (1–2 предложения + опц. ограничения).")
    p.add_argument("--idea-file", help="Файл с текстом идеи ('-' — читать из stdin).")
    p.add_argument("--output", "-o", default="output", help="Каталог для артефактов (по умолчанию: output).")
    p.add_argument("--backend", choices=("api", "cli"),
                   help="Бэкенд: api (Anthropic SDK, нужен ключ) или cli (локальный claude, без ключа).")
    p.add_argument("--model", help=f"ID модели Claude (по умолчанию: {DEFAULT_MODEL}).")
    p.add_argument("--effort", choices=VALID_EFFORTS, help="Усилие рассуждения (по умолчанию: high).")
    p.add_argument("--max-tokens", type=int, help="Лимит токенов ответа на ноду (по умолчанию: 32000).")
    p.add_argument("--no-thinking", action="store_true", help="Отключить adaptive thinking.")
    p.add_argument("--minimal", action="store_true", help="Минимальный пайплайн: бриф → story map → вайрфреймы.")
    p.add_argument("--skills", help="Свой набор скиллов через запятую (имена папок).")
    p.add_argument("--skills-dir", help="Каталог со скиллами (по умолчанию: skills/).")
    p.add_argument("--dry-run", action="store_true", help="Показать план исполнения без вызовов API.")
    p.add_argument("--list-skills", action="store_true", help="Показать список доступных скиллов и выйти.")
    return p.parse_args(argv)


def _select_skill_names(args) -> list[str]:
    if args.skills:
        return [s.strip() for s in args.skills.split(",") if s.strip()]
    if args.minimal:
        return list(MINIMAL_SKILLS)
    return list(PIPELINE_SKILLS)


def _read_idea(args) -> str:
    if args.idea:
        return args.idea.strip()
    if args.idea_file:
        if args.idea_file == "-":
            return sys.stdin.read().strip()
        return Path(args.idea_file).read_text(encoding="utf-8").strip()
    return ""


def _cmd_list_skills(cfg: Config) -> int:
    print(f"Скиллы в {cfg.skills_dir}:\n")
    for folder in sorted(p.name for p in cfg.skills_dir.iterdir() if (p / "SKILL.md").exists()):
        skills = load_skills(cfg.skills_dir, [folder])
        skill = next(iter(skills.values()))
        tools = f"  инструменты: {', '.join(skill.tools)}" if skill.tools else ""
        print(f"  • {skill.name}")
        print(f"      вход: {skill.inputs or ['—']}  →  выход: {skill.outputs or ['—']}{tools}")
    print(f"\nПолный конвейер: {', '.join(PIPELINE_SKILLS)}")
    print(f"Минимальный:     {', '.join(MINIMAL_SKILLS)}")
    return 0


def _print_plan(skills, plan) -> None:
    print("План исполнения (волны параллелизма):\n")
    for i, level in enumerate(plan.levels, 1):
        print(f"  Волна {i}:")
        for name in level:
            skill = skills[name]
            deps = plan.deps[name]
            dep_str = ", ".join(sorted(deps)) if deps else "idea"
            tools = f"  [web_search]" if "web_search" in skill.tools else ""
            print(f"    ▪ {name}: {list(skill.inputs)} → {list(skill.outputs)}"
                  f"  (после: {dep_str}){tools}")
    print()


def _fmt_tokens(n: int) -> str:
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def _make_reporter():
    """Печатает прогресс из главного потока по мере событий конвейера."""
    def on_event(kind: str, node: str, **info) -> None:
        if kind == "start":
            print(f"  ▶  {node} …", flush=True)
        elif kind == "done":
            r = info["result"]
            extra = f", веб-поиск ×{r.web_searches}" if r.web_searches else ""
            warn = "  ⚠ обрыв по лимиту" if r.truncated else ""
            if r.stop_reason == "refusal":
                warn = "  ⚠ отказ модели (refusal)"
            print(f"  ✓  {node} — {r.elapsed:.1f}s, "
                  f"{_fmt_tokens(r.output_tokens)} вых. токенов{extra}{warn}", flush=True)
        elif kind == "error":
            print(f"  ✗  {node} — ошибка: {info['error']}", flush=True)
        elif kind == "skipped":
            print(f"  ⏭  {node} — пропущена (упала зависимость)", flush=True)
    return on_event


def _print_summary(cfg: Config, result) -> None:
    print("\n" + "=" * 60)
    total_in = sum(r.input_tokens for r in result.results.values())
    total_out = sum(r.output_tokens for r in result.results.values())
    searches = sum(r.web_searches for r in result.results.values())

    print(f"Готово за {result.elapsed:.1f}s · "
          f"нод: {len(result.results)}/{len(result.results)+len(result.failed)+len(result.skipped)} · "
          f"токены: {_fmt_tokens(total_in)} вход / {_fmt_tokens(total_out)} выход"
          + (f" · веб-поиск ×{searches}" if searches else ""))

    actual_cost = sum(r.cost_usd for r in result.results.values())
    if actual_cost > 0:
        # CLI-бэкенд отдаёт реальную стоимость по каждой ноде.
        print(f"Стоимость (по данным CLI): ≈ ${actual_cost:.3f}")
    else:
        price = PRICES.get(cfg.model)
        if price:
            cost = (total_in * price[0] + total_out * price[1]) / 1_000_000
            note = " (+ веб-поиск)" if searches else ""
            print(f"Оценка стоимости токенов ({cfg.model}): ≈ ${cost:.3f}{note}")

    if result.failed:
        print(f"\n⚠ Упавшие ноды: {', '.join(result.failed)}")
    if result.skipped:
        print(f"⚠ Пропущенные ноды: {', '.join(result.skipped)}")


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cfg = Config.from_env_and_args(args)
    except ValueError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    if not cfg.skills_dir.exists():
        print(f"Каталог скиллов не найден: {cfg.skills_dir}", file=sys.stderr)
        return 2

    if args.list_skills:
        return _cmd_list_skills(cfg)

    # Загрузка выбранного набора скиллов и построение плана.
    try:
        skill_names = _select_skill_names(args)
        skills = load_skills(cfg.skills_dir, skill_names)
        plan = build_plan(skills)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Ошибка сборки конвейера: {exc}", file=sys.stderr)
        return 2

    idea = _read_idea(args)
    if not idea and not args.dry_run:
        print("Нужна идея продукта: передайте --idea \"...\" или --idea-file PATH.", file=sys.stderr)
        return 2

    if cfg.backend == "cli":
        head = f"Бэкенд: cli (локальный claude) · модель: {cfg.model} · нод: {len(skills)}"
    else:
        head = (f"Бэкенд: api · модель: {cfg.model} · усилие: {cfg.effort} · "
                f"мышление: {'adaptive' if cfg.thinking else 'off'} · нод: {len(skills)}")
    print(head + "\n")
    _print_plan(skills, plan)

    if args.dry_run:
        print("Сухой прогон (--dry-run): вызовов API не было.")
        if idea:
            print(f"\nИдея:\n{idea}")
        return 0

    print(f"Идея:\n  {idea}\n")
    print("Запуск конвейера:\n")
    reporter = _make_reporter()

    try:
        result = run_pipeline(cfg, skills, idea, on_event=reporter)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        print(f"\nСбой конвейера: {type(exc).__name__}: {msg}", file=sys.stderr)
        if "api_key" in msg.lower() or "authentication" in msg.lower():
            print("Похоже, не задан ANTHROPIC_API_KEY. Скопируйте .env.example в .env "
                  "и впишите ключ, либо выполните `ant auth login`.", file=sys.stderr)
        return 1

    _print_summary(cfg, result)

    # Сохраняем то, что успели собрать (даже при частичном провале).
    produced = {k: v for k, v in result.artifacts.items() if k != "idea" and v}
    if produced or idea:
        written = save_artifacts(cfg.output_dir, result.artifacts)
        print(f"\nАртефакты сохранены в {cfg.output_dir}/:")
        for path in written:
            label = ARTIFACT_TITLES.get(path.stem, path.stem)
            print(f"  • {path.name}  — {label}")

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
