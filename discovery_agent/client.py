"""Запуск одной ноды через Anthropic Messages API.

Системный промпт ноды = преамбула пайплайн-режима + тело SKILL.md.
Для ноды `market-research` подключается серверный инструмент web_search;
серверный tool-loop может вернуть `pause_turn` — тогда дозапрашиваем
продолжение, пока модель не закончит.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .skills import Skill

# Сколько раз готовы продолжить турн после pause_turn (серверный web search).
MAX_CONTINUATIONS = 6

PREAMBLE = """Ты — нода автономного discovery-конвейера продакт-менеджера. Работаешь строго в пайплайн-режиме.

Правила конвейера:
1. Не задавай вопросов и ничего не уточняй. Пробелы во входных данных закрывай обоснованными допущениями с пометкой [assumption].
2. Строгий выход: верни ТОЛЬКО целевой артефакт по шаблону из инструкции ниже — без вводных фраз до и без комментариев после.
3. Единый предмет: название продукта и формулировку проблемы бери из входных артефактов без искажений.
4. Язык — русский.

Следуй разделу «Режим в пайплайне (автономный)» своей инструкции. Полная инструкция ниже.

============================================================
"""


@dataclass
class NodeResult:
    """Результат работы одной ноды."""

    name: str
    text: str
    output_names: list[str] = field(default_factory=list)
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    elapsed: float = 0.0
    truncated: bool = False


def make_client(config):
    """Создаёт клиент Anthropic. Ключ берётся из ANTHROPIC_API_KEY / профиля."""
    import anthropic  # локальный импорт: --dry-run работает без установленного SDK

    return anthropic.Anthropic()


def build_system(skill: Skill) -> str:
    return PREAMBLE + skill.body


def build_user_message(skill: Skill, inputs: dict[str, str]) -> str:
    parts = ["Входные артефакты для этой ноды:\n"]
    present = [name for name in skill.inputs if (inputs.get(name) or "").strip()]
    for name in present:
        parts.append(f"\n===== {name} =====\n{inputs[name].strip()}\n")

    # Объявленные, но отсутствующие в этом прогоне входы — восстанавливаем из
    # имеющихся артефактов и помечаем допущения (как в автономном режиме).
    absent = [name for name in skill.inputs if name not in present]
    if absent:
        parts.append(
            f"\nАртефакты {', '.join(absent)} в этом прогоне недоступны — "
            f"восстанови нужное из имеющихся входов и пометь допущения [assumption].\n"
        )

    outs = ", ".join(skill.outputs) or "результат"
    parts.append(
        f"\nСформируй и верни артефакт: {outs}. "
        f"Только артефакт по шаблону скилла, без пояснений до и после."
    )
    return "".join(parts)


def _text_of(msg) -> str:
    return "".join(
        getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text"
    )


def _count_web_searches(msg) -> int:
    return sum(
        1
        for b in msg.content
        if getattr(b, "type", "") == "server_tool_use"
        and getattr(b, "name", "") == "web_search"
    )


def run_node(client, config, skill: Skill, inputs: dict[str, str]) -> NodeResult:
    """Прогоняет ноду и возвращает NodeResult с текстом артефакта и метриками."""
    t0 = time.monotonic()
    system = build_system(skill)
    messages = [{"role": "user", "content": build_user_message(skill, inputs)}]

    tools = []
    if "web_search" in skill.tools:
        tools = [
            {
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": config.web_search_max_uses,
            }
        ]

    thinking = {"type": "adaptive"} if config.thinking else {"type": "disabled"}

    in_tok = out_tok = searches = 0
    text = ""
    stop_reason = ""
    truncated = False

    for _ in range(MAX_CONTINUATIONS):
        kwargs = dict(
            model=config.model,
            max_tokens=config.max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": config.effort},
            thinking=thinking,
        )
        if tools:
            kwargs["tools"] = tools

        # Стримим ради защиты от таймаутов на длинных ответах; полный
        # результат берём через get_final_message().
        with client.messages.stream(**kwargs) as stream:
            msg = stream.get_final_message()

        usage = getattr(msg, "usage", None)
        in_tok += getattr(usage, "input_tokens", 0) or 0
        out_tok += getattr(usage, "output_tokens", 0) or 0
        searches += _count_web_searches(msg)
        stop_reason = msg.stop_reason or ""
        turn_text = _text_of(msg)

        if stop_reason == "pause_turn":
            # Серверный tool-loop приостановился — дозапрашиваем продолжение.
            messages.append({"role": "assistant", "content": msg.content})
            text = turn_text  # запомним последний фрагмент на случай обрыва
            continue

        text = turn_text
        break
    else:
        truncated = True  # исчерпали продолжения, всё ещё pause_turn

    if stop_reason == "max_tokens":
        truncated = True

    return NodeResult(
        name=skill.name,
        text=text.strip(),
        output_names=list(skill.outputs),
        stop_reason=stop_reason,
        input_tokens=in_tok,
        output_tokens=out_tok,
        web_searches=searches,
        elapsed=time.monotonic() - t0,
        truncated=truncated,
    )
