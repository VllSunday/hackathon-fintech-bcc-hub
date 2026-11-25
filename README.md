# reqaudit — агент проверки требований

ИИ-агент для [BCC-HUB x MNU FinTech Hackathon](https://bcchubagents.mnu.kz/). Принимает документ требований, выделяет отдельные требования, проверяет каждое и набор целиком по ISO/IEC/IEEE 29148:2018 (п. 5.2.5, 5.2.6) и IEEE Std 830-1998 (п. 4.3), задаёт уточняющие вопросы автору и пишет отчёт на русском языке. Каждое замечание ссылается на конкретное место документа.

Условия задачи: [BCC-HUB/ai-requirements-validator-task](https://github.com/BCC-HUB/ai-requirements-validator-task).

## Статус

Версия 0.1 — каркас. Работают приёмка файла (этап s0), паспорт прогона, трасса и сборка отчёта. Чтение, нарезка и проверки подключаются по этапам; пока этап не подключён, отчёт прямо говорит, что раздел не заполнен.

Схема всех этапов и статусы: [docs/agent-flow.html](docs/agent-flow.html).

## Быстрый старт

Нужен [uv](https://docs.astral.sh/uv/getting-started/installation/). Он сам поставит подходящий Python.

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

macOS и Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Установка зависимостей:

```bash
git clone https://github.com/VllSunday/hackathon-fintech-bcc-hub.git
cd hackathon-fintech-bcc-hub
uv sync
```

### Ключ модели

1. Получите бесплатный ключ Gemini в [Google AI Studio](https://aistudio.google.com/apikey).
2. Скопируйте `.env.example` в `.env` и впишите ключ в `GEMINI_API_KEY`.

Файл `.env` в git не попадает. В версии 0.1 модель ещё не вызывается, поэтому прогон работает и без ключа.

### Прогон

```bash
uv run reqaudit <путь к документу> <путь к отчёту>
```

Пример на документе организаторов:

```bash
uv run reqaudit corpus/inputs/organizers/requirements.md reports/report.md
```

Отчёт ляжет по указанному пути. Все промежуточные артефакты прогона лежат в `runs/<идентификатор прогона>/`:

| Файл | Что внутри |
|---|---|
| `report.md` | Копия отчёта |
| `passport.json` | Паспорт: документ, дата, модели, версия агента, отпечаток промптов |
| `s0_intake.json`, `s1_…` | Результат каждого этапа |
| `state.json` | Итоговое состояние прогона |
| `trace.jsonl` | Трасса: события этапов и вызовы модели |

Коды возврата: `0` — прогон завершён, `2` — документ не принят (отчёт с причиной всё равно записан), `3` — один из этапов упал (отчёт собран из того, что успели).

## Форматы входа

Тип файла определяется по содержимому, а не по расширению. Принимаются PDF, DOCX, Markdown, TXT, HTML и RTF; текстовые файлы — в UTF-8, UTF-16 или однобайтовых кодировках вроде cp1251. Старый `.doc`, архивы и бинарные файлы отклоняются с понятным сообщением.

## Структура

```
src/reqaudit/
  cli.py            точка входа
  pipeline.py       порядок этапов, артефакты, отчёт при любом исходе
  models.py         контракты данных между этапами
  criteria.py       критерии качества: коды, русские названия, определения для промптов
  stages/           этапы s0–s9
  prompts/          промпты, по файлу на роль
  report/           сборка отчёта
corpus/             документы для оценки и эталонная разметка
docs/agent-flow.html  схема агента
tests/
```

## Разработка

```bash
uv run pytest
uv run ruff check
```

Промпты лежат файлами в `src/reqaudit/prompts/`: инструкции на английском, всё, что пишет модель, — на русском. Почему так — в [README промптов](src/reqaudit/prompts/README.md).
