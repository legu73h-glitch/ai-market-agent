# AI-агент продакта — discovery-конвейер

Из **одной идеи продукта** собирает **пакет discovery-артефактов**: бриф,
рыночный срез, персоны, Lean Canvas, User Story Map, вайрфреймы и отчёт
симулированного custdev-интервью. Каждый артефакт делает отдельная нода —
скилл-инструкция из набора [`product_skills`](WORKFLOW.md), — а оркестратор
на Python прогоняет их через Anthropic Messages API в правильном порядке,
параллеля независимые ветки.

Порядок исполнения выводится **автоматически** из полей `inputs`/`outputs`
в front-matter каждого `SKILL.md` — граф зависимостей повторяет
[`WORKFLOW.md`](WORKFLOW.md), ничего не зашито в код руками.

```
idea
  └─(1) brief-writing ───────────────▶ brief
          ├─(2) market-research *───▶ market ──┐
          ├─(3) persona-generation ─▶ personas ┤
          │         └─(7) persona-interview ─▶ interview_report
          ├─(4) lean-canvas ◀──────── brief + market + personas
          ├─(5) user-story-mapping ◀─ brief + personas
          │         └─(6) wireframe-spec ─────▶ wireframes
          └─ семь артефактов ────────▶ output/discovery-package.md

  * ноде market-research нужен web search (серверный инструмент)
```

Волны параллелизма: `brief` → (`market` ∥ `personas`) → (`lean_canvas`, `story_map`, `interview`) → `wireframes`.

## Ноды конвейера

| # | Нода | Вход | Выход | Инструмент |
|---|------|------|-------|------------|
| 1 | `brief-writing` | idea | `brief` | — |
| 2 | `market-research` | brief | `market` | web search |
| 3 | `persona-generation` | brief | `personas` | — |
| 4 | `lean-canvas` | brief + market + personas | `lean_canvas` | — |
| 5 | `user-story-mapping` | brief + personas | `story_map` | — |
| 6 | `wireframe-spec` | story_map | `wireframes` | — |
| 7 | `persona-interview` | personas + brief | `interview_report` | — |

## Быстрый старт

```bash
# 1. Зависимости
pip install -r requirements.txt

# 2. Ключ Anthropic (любой из вариантов)
cp .env.example .env        # и впишите ANTHROPIC_API_KEY
# либо: export ANTHROPIC_API_KEY=sk-ant-...
# либо: ant auth login       (профиль подхватится автоматически)

# 3. Запуск
python main.py --idea "Сервис подписки на здоровые обеды для офисов"
```

Артефакты появятся в `output/`: по файлу на каждый (`brief.md`, `market.md`, …)
плюс сводный `discovery-package.md` с оглавлением.

Посмотреть план без вызовов API (и без ключа):

```bash
python main.py --idea "..." --dry-run
```

## Использование

```bash
# Полный конвейер (7 нод)
python main.py --idea "Идея продукта" -o out/

# Идея из файла или stdin
python main.py --idea-file idea.txt
echo "Идея" | python main.py --idea-file -

# Минимальный пайплайн: бриф → story map → вайрфреймы
python main.py --minimal --idea "..."

# Свой набор нод (граф соберётся сам)
python main.py --skills brief-writing,persona-generation,persona-interview --idea "..."

# Дешевле: другая модель / меньше усилия / без мышления
python main.py --idea "..." --model claude-sonnet-5 --effort medium

# Список доступных скиллов
python main.py --list-skills
```

### Флаги

| Флаг | Назначение | По умолчанию |
|------|-----------|--------------|
| `--idea TEXT` / `--idea-file PATH` | Идея продукта (`-` — из stdin) | — |
| `--output, -o DIR` | Каталог для артефактов | `output` |
| `--model ID` | ID модели Claude | `claude-opus-5` |
| `--effort {low,medium,high,xhigh,max}` | Усилие рассуждения | `high` |
| `--max-tokens N` | Лимит токенов ответа на ноду | `32000` |
| `--no-thinking` | Отключить adaptive thinking | (включено) |
| `--minimal` | Пайплайн из 3 нод | — |
| `--skills a,b,c` | Свой набор нод | полный |
| `--skills-dir DIR` | Каталог со скиллами | `skills` |
| `--dry-run` | Показать план без вызовов API | — |
| `--list-skills` | Список скиллов и выход | — |

Те же значения можно задать через окружение/`.env`: `DISCOVERY_MODEL`,
`DISCOVERY_EFFORT`, `DISCOVERY_MAX_TOKENS`.

## Как это устроено

- **Граф из данных.** `discovery_agent/skills.py` читает front-matter каждого
  `SKILL.md` (`inputs`, `outputs`, `metadata.tools`). `pipeline.py` строит по
  ним DAG: нода зависит от тех, кто производит её входы; `idea` — внешний вход.
  Независимые ноды идут параллельно (`ThreadPoolExecutor`).
- **Автономный режим.** Системный промпт ноды = короткая преамбула
  пайплайн-режима (не задавать вопросов, строгий выход, русский язык) + тело
  `SKILL.md`. Скиллы содержат раздел «Режим в пайплайне (автономный)» —
  оркестратор явно на него опирается. Пробелы во входе нода закрывает
  допущениями с пометкой `[assumption]`.
- **Необязательные входы.** Если в выбранном наборе какой-то вход никто не
  производит (например, `personas` в минимальном пайплайне) — он считается
  необязательным: нода отработает по имеющимся артефактам. Требуется хотя бы
  один доступный вход, иначе нода оторвана от конвейера и сборка падает с
  понятной ошибкой.
- **Web search + pause_turn.** `market-research` подключает серверный
  инструмент `web_search`. Серверный tool-loop может вернуть `pause_turn` —
  оркестратор дозапрашивает продолжение, пока модель не завершит отчёт.
- **Стриминг.** Каждый вызов идёт через `messages.stream(...)` +
  `get_final_message()` — защита от таймаутов на длинных ответах.
- **Устойчивость.** Упавшая нода не роняет весь прогон: независимые ветки
  доходят до конца, зависимые помечаются пропущенными, а всё уже собранное
  сохраняется на диск.

## Что на выходе

```
output/
├── brief.md
├── market.md
├── personas.md
├── lean_canvas.md
├── story_map.md
├── wireframes.md
├── interview_report.md
└── discovery-package.md   ← всё вместе, с оглавлением
```

В конце прогона печатается сводка: время, токены и грубая оценка стоимости
по текущим ценам модели.

## Стоимость

Полный прогон — это 7+ вызовов модели (плюс веб-поиск для рыночного среза).
На `claude-opus-5` это обычно порядка нескольких десятков тысяч выходных
токенов суммарно. Рычаги экономии: `--model claude-sonnet-5`,
`--effort medium`, `--minimal` или свой сокращённый `--skills`.

## Расширение

Чтобы добавить ноду — положите папку со `SKILL.md` в `skills/`, пропишите в
её front-matter `inputs`/`outputs` (и `metadata.tools`, если нужен веб-поиск)
и добавьте имя папки в `PIPELINE_SKILLS` (`discovery_agent/skills.py`) или
передайте через `--skills`. Граф пересоберётся сам.

В `skills/` уже лежат два дополнительных скилла — `user-journey-map` и
`information-architecture`, — они не входят в discovery-конвейер, но доступны
для отдельного использования.

## Тесты

Оффлайн-проверка всего конвейера на замоканном клиенте (без сети и ключа):

```bash
python -m unittest tests.test_offline -v
```

Проверяет порядок волн, параллельный запуск, обработку `pause_turn`, форму
запроса и сборку пакета.

## Состав репозитория

```
ai-market-agent/
├── main.py                    # CLI
├── discovery_agent/
│   ├── config.py              # конфиг (модель, усилие, пути, .env)
│   ├── skills.py              # загрузка скиллов + разбор front-matter
│   ├── client.py              # запуск ноды: промпт, web search, pause_turn, стриминг
│   ├── pipeline.py            # граф зависимостей + параллельный исполнитель
│   └── artifacts.py           # сохранение артефактов и сводного пакета
├── skills/                    # SKILL.md всех нод (+ examples.md)
├── tests/test_offline.py      # оффлайн-проверка конвейера
├── WORKFLOW.md                # исходная схема связки нод
├── requirements.txt
└── .env.example
```

## Источник скиллов

Скиллы взяты из набора **Product Skills** (Academy of Yandex AI Studio) —
готовые инструкции агентов под задачи discovery и product work. Общая схема
связки нод — в [`WORKFLOW.md`](WORKFLOW.md).
