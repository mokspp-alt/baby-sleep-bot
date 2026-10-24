"""Свободный диалог через Claude: родители пишут как угодно, модель отвечает и, когда нужно,
меняет данные бота через инструменты (отметки сна, время сна и отбоя на сегодня, настройки).

Если ключа нет или API недоступен, бот работает на правилах из parsing.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta

import anthropic

log = logging.getLogger(__name__)

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
EFFORT = os.environ.get("CLAUDE_EFFORT", "low")
HISTORY_TURNS = 10  # сколько последних обменов репликами помнить


SYSTEM = """\
Ты — заботливый помощник родителей в Telegram-боте, который ведёт сон ребёнка.
Родители пишут или говорят голосом (текст — расшифровка речи, в ней бывают ошибки).

Разговаривай свободно: отвечай на любые вопросы — о сне, режиме, развитии, самочувствии,
бытовые и не только. Поддерживай, уточняй, если что-то неясно. Не навязывай тему сна,
если спрашивают о другом.

Данные бота меняй только инструментами и только когда родитель этого хочет или сообщает факт:
- Проснулся / уснул → record_event для каждого события. Время из сообщения переводи в ЧЧ:ММ
  («час назад», «в половине девятого»); без времени — "now". «Уснул» без уточнения — kind "sleep",
  бот сам поймёт, дневной это сон или ночной; явно ночной — "night", явно дневной — "nap".
  О вчерашнем вечере, рассказанном утром («вчера уснул в 22:30»), — day "yesterday".
- Ночные пробуждения, после которых ребёнок снова уснул, не записывай — это часть ночи.
- Ошибка в отметке («не в 8, а в 8:20», «это был дневной сон») → update_event по номеру #id из
  блока «Состояние»; лишняя отметка → delete_event.
- «Сегодня уложим днём в 13:00», «отбой сегодня в 20:30» → set_plan_time. «Верни как было» → "auto".
- Изменились имя, дата рождения, число дневных снов, время напоминаний, часовой пояс →
  update_settings.
- Никогда не придумывай события, время и факты, которых не было в сообщении. Если не уверен,
  какое время имелось в виду, переспроси, а не записывай наугад.
- Если просят то, чего бот не умеет, честно скажи об этом.

Фото и файлы (скриншоты других приложений, дневник сна, выписки, анализы, таблицы):
- Внимательно извлеки данные и кратко перескажи главное; на вопрос в подписи ответь по файлу.
- Если в файле записи сна: одну-две свежие записи, которые явно нужно отметить, — запиши.
  Если записей много (журнал за несколько дней), сначала перечисли найденное по дням и
  спроси, записать ли; после согласия запиши их, указывая день датой.
- Не выдумывай то, что не видно или не читается, — скажи, что именно разобрать не удалось.
- Медицинские документы поясняй простыми словами, без диагнозов; значения вне нормы
  и всё тревожное — повод показать педиатру.

Про режим: план в блоке «Состояние» уже учитывает возраст, длину ночи, реальный дневной сон
и время, назначенное родителями, — опирайся на него, а не пересчитывай сам. Нормы — это
ориентир, а не правило. Медицинских диагнозов не ставь; при тревожных симптомах (высокая
температура, вялость, проблемы с дыханием и т. п.) советуй обратиться к педиатру.

Стиль: по-русски, тепло и по делу. Обычно 1–4 предложения; на сложный вопрос можно подробнее.
Без markdown: Telegram покажет звёздочки как есть. Называй ребёнка по имени и согласуй слова
с его полом. Время пиши как 8:20. После изменений коротко подтверди, что сделал.
"""

# Кэш запросов (prompt caching). Инструкции и инструменты не меняются и одинаковы для всех
# чатов — держим их час: родители пишут с перерывами. Историю и хвост запроса — 5 минут:
# они пригодятся, если сообщения идут подряд, и внутри одного ответа, где бот вызывает
# инструменты и делает несколько запросов подряд.
CACHED_SYSTEM = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]


def _with_history_breakpoint(history: list[dict]) -> list[dict]:
    """Копия истории с меткой кэша на последней реплике; сама история не меняется."""
    if not history:
        return []
    last = history[-1]
    marked = {"role": last["role"], "content": [
        {"type": "text", "text": last["content"], "cache_control": {"type": "ephemeral"}}]}
    return history[:-1] + [marked]


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {"type": "object", "properties": properties,
                         "required": list(properties), "additionalProperties": False},
    }


_TIME = {"type": "string", "description": "ЧЧ:ММ по местному времени или \"now\""}
_DAY = {"type": "string",
        "description": "К какому дню относится время: today, yesterday или дата ДД.ММ.ГГГГ"}

TOOLS = [
    _tool("record_event", "Записать, что ребёнок проснулся или уснул.", {
        "kind": {"type": "string", "enum": ["wake", "sleep", "nap", "night"],
                 "description": "wake — проснулся; sleep — уснул (тип сна определит бот); "
                                "nap — уснул днём; night — уснул на ночь"},
        "time": _TIME,
        "day": _DAY,
    }),
    _tool("update_event", "Исправить отметку по её номеру #id из состояния.", {
        "event_id": {"type": "integer"},
        "kind": {"type": "string", "enum": ["keep", "wake", "sleep", "nap", "night"],
                 "description": "Новый тип или keep — не менять"},
        "time": {"type": "string", "description": "Новое время ЧЧ:ММ или keep — не менять"},
        "day": _DAY,
    }),
    _tool("delete_event", "Удалить ошибочную отметку по номеру #id.", {
        "event_id": {"type": "integer"},
    }),
    _tool("set_plan_time",
          "Назначить на сегодня время ближайшего дневного сна или отбоя вместо расчётного; "
          "напоминания придут к нему. time \"auto\" возвращает расчёт.", {
              "target": {"type": "string", "enum": ["nap", "bedtime"]},
              "time": {"type": "string", "description": "ЧЧ:ММ или auto"},
          }),
    _tool("update_settings", "Изменить данные ребёнка или настройки бота.", {
        "setting": {"type": "string", "enum": [
            "child_name", "gender", "birth_date", "naps_per_day", "usual_wake",
            "remind_before_minutes", "morning_check", "timezone"]},
        "value": {"type": "string", "description":
                  "child_name — имя; gender — m или f; birth_date — ДД.ММ.ГГГГ; naps_per_day — "
                  "0–3 или auto; usual_wake — ЧЧ:ММ или none; remind_before_minutes — 0–180; "
                  "morning_check — ЧЧ:ММ или off; timezone — например Europe/Moscow"},
    }),
]


class Assistant:
    def __init__(self, api_key: str, proxy: str | None = None, model: str = MODEL,
                 effort: str = EFFORT) -> None:
        self.model, self.effort = model, effort
        http_client = anthropic.DefaultAsyncHttpxClient(proxy=proxy) if proxy else None
        self.client = anthropic.AsyncAnthropic(api_key=api_key, http_client=http_client,
                                               timeout=90.0)
        self.history: dict[int, list[dict]] = {}

    @classmethod
    def from_env(cls) -> Assistant | None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        proxy = os.environ.get("ANTHROPIC_PROXY") or os.environ.get("TELEGRAM_PROXY") or None
        return cls(key, proxy)

    async def handle(self, chat_id: int, text: str, state: str, tools,
                     attachments: list[dict] | None = None, label: str = "") -> str:
        """state — описание дня для модели; tools — объект с методами-инструментами
        (record_event, update_event, …) и state(), работающий с базой бота.
        attachments — блоки фото и файлов (attachments.to_block), label — их название для истории."""
        history = self.history.setdefault(chat_id, [])
        prompt = f"<Состояние>\n{state}\n</Состояние>\n\nСообщение родителя: {text}"
        content = [*(attachments or []), {"type": "text", "text": prompt}]
        messages = _with_history_breakpoint(history) + [{"role": "user", "content": content}]
        extra = {}
        if not self.model.startswith("claude-haiku"):
            # Если модель откажется отвечать, API сам повторит запрос на другой. У Haiku такого нет.
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        for _ in range(10):
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                system=CACHED_SYSTEM,
                tools=TOOLS,
                messages=messages,
                cache_control={"type": "ephemeral"},  # хвост: переиспользуется в цикле инструментов
                output_config={"effort": self.effort},
                **extra,
            )
            if response.stop_reason == "refusal":
                raise RuntimeError("Claude отказался отвечать")
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                break
            messages.append({"role": "user", "content": [
                self._run_tool(block, tools) for block in response.content if block.type == "tool_use"
            ]})

        reply = "\n\n".join(b.text.strip() for b in response.content if b.type == "text").strip()
        reply = reply or "Готово."
        # В памяти держим только текст реплик, без служебного состояния и инструментов.
        # Сами файлы в историю не попадают (дорого), только пометка — найденное есть в ответе.
        said = f"[{label}] {text}".strip() if label else text
        history += [{"role": "user", "content": said}, {"role": "assistant", "content": reply}]
        del history[:-HISTORY_TURNS * 2]
        return reply

    @staticmethod
    def _run_tool(block, tools) -> dict:
        try:
            args = block.input if isinstance(block.input, dict) else json.loads(block.input)
            if block.name not in {t["name"] for t in TOOLS}:
                raise ValueError(f"неизвестный инструмент {block.name}")
            result = getattr(tools, block.name)(**args)
            return {"type": "tool_result", "tool_use_id": block.id,
                    "content": f"{result}\n\nОбновлённое состояние:\n{tools.state()}"}
        except Exception as e:  # ошибку отдаём модели — она объяснит родителю или переспросит
            return {"type": "tool_result", "tool_use_id": block.id,
                    "content": f"Ошибка: {e}", "is_error": True}


def parse_hhmm(value: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*", value)
    return (int(m.group(1)), int(m.group(2))) if m else None


def resolve_time(time: str, day: str, now: datetime) -> datetime:
    """Время от модели → момент в часовом поясе now."""
    if time.strip().lower() == "now":
        return now
    hm = parse_hhmm(time)
    if hm is None:
        raise ValueError(f"не понимаю время «{time}», нужно ЧЧ:ММ")
    at = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if day == "yesterday":
        at -= timedelta(days=1)
    elif m := re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s*", day):
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            at = at.replace(year=year, month=int(m.group(2)), day=int(m.group(1)))
        except ValueError:
            raise ValueError(f"нет такой даты «{day}»") from None
        if not m.group(3) and at > now + timedelta(minutes=5):
            at = at.replace(year=year - 1)  # «31.12», сказанное в январе
    elif at > now + timedelta(minutes=5):
        at -= timedelta(days=1)  # «уснул в 23:40», сказанное после полуночи
    return at
