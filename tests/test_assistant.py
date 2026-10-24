"""Свободный диалог: подставной клиент Claude возвращает заранее заданные ответы."""

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sleepbot import bot
from sleepbot.assistant import Assistant, resolve_time
from sleepbot.schedule import NIGHT, WAKE
from sleepbot.storage import Storage

TZ = ZoneInfo("Europe/Moscow")


def tool_use(id, name, **inp):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=inp)


def text(t):
    return SimpleNamespace(type="text", text=t)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})  # снимок на момент вызова
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        stop, content = item
        return SimpleNamespace(stop_reason=stop, content=content)


def fake_assistant(responses):
    a = Assistant.__new__(Assistant)
    a.history = {}
    a.client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(responses)))
    return a


def configured_storage():
    s = Storage(":memory:")
    s.update_chat(1, child_name="Лёва", gender="m", birth_date=date(2024, 8, 1))
    return s


def say(s, assistant, msg):
    sent = []

    async def reply_text(t, reply_markup=None):
        sent.append(t)

    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1, type="private"),
                             effective_message=SimpleNamespace(reply_text=reply_text))
    context = SimpleNamespace(application=SimpleNamespace(
        bot_data={"storage": s, "assistant": assistant}))
    asyncio.run(bot.handle_text(update, context, msg))
    return "\n".join(sent)


def test_free_text_records_wake():
    s = configured_storage()
    a = fake_assistant([
        ("tool_use", [tool_use("t1", "record_event", kind="wake", time="00:01", day="today")]),
        ("end_turn", [text("Записал подъём. Сочувствую, что ночь была беспокойной.")]),
    ])
    reply = say(s, a, "сегодня Лёва плохо спал и проснулся в 0:01")
    assert "Сочувствую" in reply and "📅 План на сегодня" in reply
    [event] = s.events(1, datetime(2000, 1, 1, tzinfo=TZ))
    assert event.kind == WAKE
    assert event.ts.astimezone(TZ).strftime("%H:%M") == "00:01"

    calls = a.client.beta.messages.calls
    first = calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default"
    assert "Лёва, мальчик" in first["messages"][-1]["content"]
    tool_result = calls[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "t1" and "Записано: подъём в 00:01" in tool_result["content"]
    # В памяти диалога — только текст реплик.
    assert a.history[1] == [
        {"role": "user", "content": "сегодня Лёва плохо спал и проснулся в 0:01"},
        {"role": "assistant", "content": "Записал подъём. Сочувствую, что ночь была беспокойной."},
    ]


def test_correction_undoes_and_rerecords():
    s = configured_storage()
    s.add_event(1, NIGHT, datetime.now(TZ) - timedelta(minutes=30))
    a = fake_assistant([
        ("tool_use", [tool_use("t1", "undo_last_event"),
                      tool_use("t2", "record_event", kind="night", time="21:30", day="yesterday")]),
        ("end_turn", [text("Исправил: уснул вчера в 21:30.")]),
    ])
    say(s, a, "нет, он уснул вчера в 21:30")
    [event] = s.events(1, datetime(2000, 1, 1, tzinfo=TZ))
    assert event.kind == NIGHT
    assert event.ts.astimezone(TZ) == resolve_time("21:30", "yesterday", datetime.now(TZ))


def test_api_failure_falls_back_to_rules():
    s = configured_storage()
    a = fake_assistant([ConnectionError("нет сети")])
    reply = say(s, a, "проснулся")
    assert "Лёва проснулся в" in reply
    assert [e.kind for e in s.events(1, datetime(2000, 1, 1, tzinfo=TZ))] == [WAKE]


def test_buttons_skip_claude():
    s = configured_storage()
    a = fake_assistant([])
    say(s, a, bot.BTN_WAKE)
    assert a.client.beta.messages.calls == []


def test_resolve_time():
    now = datetime(2026, 10, 24, 0, 20, tzinfo=TZ)
    assert resolve_time("23:40", "today", now) == datetime(2026, 10, 23, 23, 40, tzinfo=TZ)
    assert resolve_time("8:05", "yesterday", now) == datetime(2026, 10, 23, 8, 5, tzinfo=TZ)
    assert resolve_time("now", "today", now) == now
