"""Конфигурация конвейера: модель, усилие рассуждения, лимиты, пути."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Default to the latest, most capable Claude model. Override per-run with
# --model / DISCOVERY_MODEL when cost matters (e.g. claude-sonnet-5).
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
DEFAULT_MAX_TOKENS = 32000
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
VALID_BACKENDS = ("api", "cli")

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Минимальный загрузчик .env (строки KEY=VALUE), без внешних зависимостей.

    Не перетирает переменные, уже заданные в окружении.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Config:
    """Параметры прогона конвейера."""

    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    max_tokens: int = DEFAULT_MAX_TOKENS
    thinking: bool = True
    skills_dir: Path = field(default_factory=lambda: _REPO_ROOT / "skills")
    output_dir: Path = field(default_factory=lambda: Path("output"))
    max_workers: int = 4
    web_search_max_uses: int = 8
    # Бэкенд исполнения нод: "api" (Anthropic SDK, нужен ключ) или
    # "cli" (локальный `claude` CLI, ключ не нужен).
    backend: str = "api"
    cli_bin: str = field(default_factory=lambda: os.environ.get("CLAUDE_CODE_EXECPATH") or "claude")
    cli_timeout: int = 900
    cli_retries: int = 3  # повторы CLI-ноды при временных сбоях (TLS/сеть/overload)

    @classmethod
    def from_env_and_args(cls, args) -> "Config":
        """Собирает конфиг: значения по умолчанию → .env/окружение → аргументы CLI."""
        # .env берём из текущей директории и из корня репозитория.
        _load_dotenv(Path.cwd() / ".env")
        _load_dotenv(_REPO_ROOT / ".env")

        cfg = cls()
        cfg.model = os.environ.get("DISCOVERY_MODEL", cfg.model)
        cfg.effort = os.environ.get("DISCOVERY_EFFORT", cfg.effort)
        cfg.backend = os.environ.get("DISCOVERY_BACKEND", cfg.backend)
        if os.environ.get("DISCOVERY_MAX_TOKENS"):
            cfg.max_tokens = int(os.environ["DISCOVERY_MAX_TOKENS"])

        if getattr(args, "backend", None):
            cfg.backend = args.backend
        if getattr(args, "model", None):
            cfg.model = args.model
        if getattr(args, "effort", None):
            cfg.effort = args.effort
        if getattr(args, "max_tokens", None):
            cfg.max_tokens = args.max_tokens
        if getattr(args, "no_thinking", False):
            cfg.thinking = False
        if getattr(args, "skills_dir", None):
            cfg.skills_dir = Path(args.skills_dir)
        if getattr(args, "output", None):
            cfg.output_dir = Path(args.output)

        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.backend not in VALID_BACKENDS:
            raise ValueError(
                f"backend должен быть одним из {VALID_BACKENDS}, получено: {self.backend!r}"
            )
        if self.effort not in VALID_EFFORTS:
            raise ValueError(
                f"effort должен быть одним из {VALID_EFFORTS}, получено: {self.effort!r}"
            )
        if self.max_tokens < 1024:
            raise ValueError("max_tokens должен быть не меньше 1024")
        # На моделях Opus/Sonnet 5 отключённое мышление недопустимо при
        # усилии xhigh/max (API вернёт 400). Оставляем комбинацию валидной.
        if not self.thinking and self.effort in ("xhigh", "max"):
            self.effort = "high"
