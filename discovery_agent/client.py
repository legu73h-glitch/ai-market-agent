"""Запуск одной ноды через Anthropic Messages API.

Системный промпт ноды = преамбула пайплайн-режима + тело SKILL.md.
Для ноды `market-research` подключается серверный инструмент web_search;
серверный tool-loop может вернуть `pause_turn` — тогда дозапрашиваем
продолжение, пока модель не закончит.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field

from .skills import Skill

# Сколько раз готовы продолжить турн после pause_turn (серверный web search).
MAX_CONTINUATIONS = 6

# Инструменты, которые нода не должна трогать в CLI-бэкенде (чистая генерация).
_CLI_BASE_DISALLOWED = [
    "Bash", "Edit", "MultiEdit", "Write", "Read", "Glob", "Grep",
    "Task", "TodoWrite", "NotebookEdit", "BashOutput", "KillShell", "SlashCommand",
]

PREAMBLE = """Ты — нода автономного discovery-конвейера продакт-менеджера. Работаешь строго в пайплайн-режиме.

Правила конвейера:
1. Не задавай вопросов и ничего не уточняй. Пробелы во входных данных закрывай обоснованными допущениями с пометкой [assumption].
2. Строгий выход: верни ТОЛЬКО целевой артефакт по шаблону из инструкции ниже — без вводных фраз до и без комментариев после.
3. Не оборачивай весь ответ в код-блок с тройными кавычками (```). Пиши markdown напрямую.
4. Единый предмет: название продукта и формулировку проблемы бери из входных артефактов без искажений.
5. Язык — русский.

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
    cost_usd: float = 0.0  # заполняется CLI-бэкендом (для API оценивается в main)


def make_client(config):
    """Готовит клиент для выбранного бэкенда.

    backend="api"  → клиент Anthropic (ключ из ANTHROPIC_API_KEY / профиля).
    backend="cli"  → None (ноды исполняет локальный `claude` CLI, ключ не нужен).
    """
    if getattr(config, "backend", "api") == "cli":
        return None
    import anthropic  # локальный импорт: --dry-run/CLI работают без установленного SDK

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


def strip_code_fence(text: str) -> str:
    """Снимает единственную внешнюю обёртку ```…``` (если модель обернула весь ответ).

    Оставляет как есть, если внешней обёртки нет или внутри есть другие ```-блоки
    (тогда снятие первой/последней строки исказило бы контент).
    """
    t = (text or "").strip()
    lines = t.splitlines()
    if len(lines) >= 2 and lines[0].lstrip().startswith("```") and lines[-1].strip() == "```":
        fences = sum(1 for ln in lines if ln.lstrip().startswith("```"))
        if fences == 2:
            return "\n".join(lines[1:-1]).strip()
    return t


def run_node(client, config, skill: Skill, inputs: dict[str, str]) -> NodeResult:
    """Диспетчер: исполняет ноду выбранным бэкендом (api | cli) и чистит артефакт."""
    if getattr(config, "backend", "api") == "cli":
        result = run_node_cli(config, skill, inputs)
    else:
        result = run_node_api(client, config, skill, inputs)
    result.text = strip_code_fence(result.text)
    return result


def run_node_api(client, config, skill: Skill, inputs: dict[str, str]) -> NodeResult:
    """Прогоняет ноду через Anthropic Messages API (стриминг + pause_turn)."""
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


# Признаки временных сбоев CLI, при которых повтор оправдан.
_CLI_TRANSIENT = (
    "self-signed certificate", "unable to connect", "connection error",
    "econnreset", "socket hang up", "overloaded", "rate limit",
    "timeout", "timed out", "502", "503", "529", "internal server error",
)


def _is_transient(message: str) -> bool:
    low = message.lower()
    return any(marker in low for marker in _CLI_TRANSIENT)


def run_node_cli(config, skill: Skill, inputs: dict[str, str]) -> NodeResult:
    """Прогоняет ноду через локальный `claude` CLI (ключ Anthropic не нужен).

    Тот же системный промпт и то же сообщение, что и в API-бэкенде. Ноде
    market-research разрешаются веб-инструменты, остальным — чистая генерация.
    Временные сбои (TLS/сеть/overload) повторяются с экспоненциальной паузой.
    """
    t0 = time.monotonic()
    retries = max(1, getattr(config, "cli_retries", 3))
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            result = _cli_attempt(config, skill, inputs)
            result.elapsed = time.monotonic() - t0
            return result
        except RuntimeError as exc:
            last_exc = exc
            if attempt < retries and _is_transient(str(exc)):
                time.sleep(2 ** attempt)
                continue
            raise
    assert last_exc is not None
    raise last_exc


def _cli_attempt(config, skill: Skill, inputs: dict[str, str]) -> NodeResult:
    """Один заход CLI-ноды (без повторов)."""
    system = build_system(skill)
    user = build_user_message(skill, inputs)
    web = "web_search" in skill.tools

    cmd = [
        config.cli_bin, "-p", user,
        "--system-prompt", system,
        "--model", config.model,
        "--output-format", "json",
        "--setting-sources", "user",
    ]
    disallowed = list(_CLI_BASE_DISALLOWED)
    if web:
        cmd += ["--allowedTools", "WebSearch", "WebFetch"]
    else:
        disallowed += ["WebSearch", "WebFetch"]
    cmd += ["--disallowedTools", *disallowed]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=config.cli_timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude CLI: таймаут {config.cli_timeout}s") from exc
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Не найден claude CLI ({config.cli_bin!r}). Установите Claude Code "
            f"или используйте --backend api с ключом ANTHROPIC_API_KEY."
        ) from exc

    out = (proc.stdout or "").strip()
    text = ""
    in_tok = out_tok = searches = 0
    cost = 0.0
    stop_reason = ""

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = None

    if data is not None:
        text = (data.get("result") or "").strip()
        usage = data.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        searches = int((usage.get("server_tool_use") or {}).get("web_search_requests") or 0)
        cost = float(data.get("total_cost_usd") or 0.0)
        stop_reason = data.get("stop_reason") or ("error" if data.get("is_error") else "end_turn")
        if data.get("is_error"):
            raise RuntimeError(f"claude CLI вернул ошибку: {text or data.get('subtype')}")
    else:
        text = out
        stop_reason = "end_turn"

    if not text:
        err = (proc.stderr or "").strip()[:400]
        raise RuntimeError(f"claude CLI (rc={proc.returncode}) без результата: {err}")

    return NodeResult(
        name=skill.name,
        text=text.strip(),
        output_names=list(skill.outputs),
        stop_reason=stop_reason,
        input_tokens=in_tok,
        output_tokens=out_tok,
        web_searches=searches,
        elapsed=0.0,  # проставляется обёрткой run_node_cli
        truncated=(stop_reason == "max_tokens"),
        cost_usd=cost,
    )
