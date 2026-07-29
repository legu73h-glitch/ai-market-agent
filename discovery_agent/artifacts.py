"""Сохранение артефактов на диск: отдельные файлы + сводный пакет."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

# Человекочитаемые заголовки и канонический порядок артефактов в пакете.
ARTIFACT_TITLES = {
    "idea": "Идея",
    "brief": "Бриф",
    "market": "Рыночный срез (Market Research)",
    "personas": "Персоны",
    "lean_canvas": "Lean Canvas",
    "story_map": "User Story Map",
    "wireframes": "Вайрфреймы",
    "interview_report": "Отчёт custdev-интервью",
}

ARTIFACT_ORDER = [
    "idea",
    "brief",
    "market",
    "personas",
    "lean_canvas",
    "story_map",
    "wireframes",
    "interview_report",
]


def _ordered(names) -> list[str]:
    known = [a for a in ARTIFACT_ORDER if a in names]
    extra = [a for a in names if a not in ARTIFACT_ORDER]
    return known + sorted(extra)


def save_artifacts(output_dir: Path, artifacts: dict[str, str]) -> list[Path]:
    """Пишет каждый артефакт в `<name>.md` и общий `discovery-package.md`."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for name in _ordered(artifacts.keys()):
        if name == "idea":
            continue
        content = (artifacts.get(name) or "").rstrip()
        if not content:
            continue
        path = output_dir / f"{name}.md"
        path.write_text(content + "\n", encoding="utf-8")
        written.append(path)

    package_path = output_dir / "discovery-package.md"
    package_path.write_text(build_package(artifacts), encoding="utf-8")
    written.append(package_path)
    return written


def build_package(artifacts: dict[str, str]) -> str:
    """Собирает единый markdown-документ со всеми артефактами и оглавлением."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        "# Discovery-пакет",
        "",
        f"> Сгенерировано автономным discovery-конвейером · {ts}",
        "",
    ]

    idea = (artifacts.get("idea") or "").strip()
    if idea:
        lines += ["## Идея", "", idea, ""]

    present = [a for a in _ordered(artifacts.keys()) if a != "idea" and (artifacts.get(a) or "").strip()]

    lines += ["## Содержание", ""]
    for name in present:
        lines.append(f"- [{ARTIFACT_TITLES.get(name, name)}](#{name})")
    lines.append("")

    for name in present:
        lines += [
            f'<a id="{name}"></a>',
            "",
            f"## {ARTIFACT_TITLES.get(name, name)}",
            "",
            (artifacts.get(name) or "").strip(),
            "",
            "---",
            "",
        ]

    return "\n".join(lines).rstrip() + "\n"
