"""Свободный диалог через Claude: «сегодня Лёва плохо спал и проснулся в 8:20» → отметка подъёма.

Модель видит текущее состояние дня и записывает события через инструменты; ответы
родителю пишет сама. Если ключа нет или API недоступен, бот работает на правилах
из parsing.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import anthropic

log = logging.getLogger(__name__)

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
HISTORY_TURNS = 6  # сколько последних реплик помнить для «а тогда во сколько отбой?»

SYSTEM = """\
Ты — помощник родителей в Telegram-боте, который следит за сном ребёнка.
Родители пишут или говорят голосом (текст — расшифровка, в ней бывают ошибки распознавания).

Что делать:
- Если родитель сообщает, что ребёнок проснулся или уснул, вызови record_event для каждого
  такого события. Время бери из сообщения; «час назад», «в половине девятого» переводи в ЧЧ:ММ.
  Если время не названо — "now".
- «Уснул» без уточнения — kind "sleep" (бот сам решит, дневной это сон или ночной). Если ясно,
  что это ночной сон («на ночь», «вчера вечером») — "night"; дневной — "nap".
- Вечерний сон, о котором говорят утром («вчера уснул в 22:30»), — kind "night", day "yesterday".
- Ночные пробуждения, после которых ребёнок снова уснул, не записывай — это часть ночи.
  Утренний подъём записывай.
- Если родитель исправляет только что сделанную отметку («нет, не в 8, а в 8:20», «ошибся»),
  сначала вызови undo_last_event, потом запиши правильно.
- Никогда не придумывай события и время, которых нет в сообщении.
- На вопросы («когда спать?», «во сколько отбой?», «сколько спал?») отвечай по данным из блока
  «Состояние». План уже учитывает возраст, длину ночи и дневной сон — не пересчитывай его сам.
- Если родитель рассказывает о самочувствии («плохо спал», «капризничает», «болеет»),
  коротко и тепло откликнись. При тревожных симптомах посоветуй обратиться к педиатру.
  Медицинских диагнозов не ставь.

Отвечай по-русски, коротко: 1–4 предложения, без markdown и списков. Называй ребёнка по имени
и согласуй слова с его полом. Время пиши как 8:20.
"""

TOOLS = [
    {
        "name": "record_event",
        "description": "Записать, что ребёнок проснулся или уснул.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["wake", "sleep", "nap", "night"],
                    "description": "wake — проснулся; sleep — уснул (тип сна определит бот); "
                                   "nap — уснул днём; night — уснул на ночь",
                },
                "time": {
                    "type": "string",
                    "description": "Время ЧЧ:ММ по местному времени или \"now\"",
                },
                "day": {
                    "type": "string",
                    "enum": ["today", "yesterday"],
                    "description": "К какому дню относится время",
                },
            },
            "required": ["kind", "time", "day"],
            "additionalProperties": False,
        },
    },
    {
        "name": "undo_last_event",
        "description": "Удалить последнюю отметку (если родитель говорит, что она ошибочная).",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "required": [],
                         "additionalProperties": False},
    },
]


@dataclass
class Turn:
    """Итог одного сообщения: ответ и id записанных событий (для кнопок исправления)."""

    text: str
    event_ids: list[int] = field(default_factory=list)


class Assistant:
    def __init__(self, api_key: str, proxy: str | None = None) -> None:
        http_client = anthropic.DefaultAsyncHttpxClient(proxy=proxy) if proxy else None
        self.client = anthropic.AsyncAnthropic(api_key=api_key, http_client=http_client,
                                               timeout=60.0)
        self.history: dict[int, list[dict]] = {}

    @classmethod
    def from_env(cls) -> Assistant | None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        proxy = os.environ.get("ANTHROPIC_PROXY") or os.environ.get("TELEGRAM_PROXY") or None
        return cls(key, proxy)

    async def handle(self, chat_id: int, text: str, state: str, tools) -> Turn:
        """state — описание дня для модели; tools — объект с record_event / undo_last_event /
        state(), выполняющий действия в базе бота."""
        history = self.history.setdefault(chat_id, [])
        messages = history + [{
            "role": "user",
            "content": f"<Состояние>\n{state}\n</Состояние>\n\nСообщение родителя: {text}",
        }]
        recorded: list[int] = []
        for _ in range(4):
            response = await self.client.beta.messages.create(
                model=MODEL,
                max_tokens=8000,
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                raise RuntimeError("Claude отказался отвечать")
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                break
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                try:
                    if block.name == "record_event":
                        args = block.input if isinstance(block.input, dict) else json.loads(block.input)
                        event_id, result = tools.record_event(args["kind"], args["time"], args["day"])
                        recorded.append(event_id)
                    elif block.name == "undo_last_event":
                        result = tools.undo_last_event()
                    else:
                        raise ValueError(f"Неизвестный инструмент {block.name}")
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": result + "\n\nОбновлённое состояние:\n" + tools.state()})
                except Exception as e:  # ошибку отдаём модели, она объяснит родителю
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"Ошибка: {e}", "is_error": True})
            messages.append({"role": "user", "content": results})

        reply = " ".join(b.text for b in response.content if b.type == "text").strip()
        # В памяти держим только текст реплик, без служебного состояния и инструментов.
        history += [{"role": "user", "content": text},
                    {"role": "assistant", "content": reply or "Хорошо."}]
        del history[:-HISTORY_TURNS * 2]
        return Turn(reply or "Записал.", recorded)


def parse_hhmm(value: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*", value)
    return (int(m.group(1)), int(m.group(2))) if m else None


def resolve_time(time: str, day: str, now: datetime) -> datetime:
    """Время от модели → момент в часовом поясе now."""
    if time.strip().lower() == "now":
        return now
    hm = parse_hhmm(time)
    if hm is None:
        raise ValueError(f"Не понимаю время «{time}», нужно ЧЧ:ММ")
    at = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if day == "yesterday":
        at -= timedelta(days=1)
    elif at > now + timedelta(minutes=5):
        at -= timedelta(days=1)  # «уснул в 23:40», сказанное после полуночи
    return at
