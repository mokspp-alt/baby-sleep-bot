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
    assert first["model"] == "claude-sonnet-5-5"
    assert first["fallbacks"] == "default"
    assert "Лёва, мальчик" in first["messages"][-1]["content"]
    tool_result = calls[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "t1" and "проснулся(ась) в 00:01" in tool_result["content"]
    # В памяти диалога — только текст реплик.
    assert a.history[1] == [
        {"role": "user", "content": "сегодня Лёва плохо спал и проснулся в 0:01"},
        {"role": "assistant", "content": "Записал подъём. Сочувствую, что ночь была беспокойной."},
    ]


def test_correction_updates_event_by_id():
    s = configured_storage()
    event_id = s.add_event(1, NIGHT, datetime.now(TZ) - timedelta(minutes=30))
    a = fake_assistant([
        ("tool_use", [tool_use("t1", "update_event", event_id=event_id, kind="keep",
                               time="21:30", day="yesterday")]),
        ("end_turn", [text("Исправил: уснул вчера в 21:30.")]),
    ])
    reply = say(s, a, "нет, он уснул вчера в 21:30")
    assert f"#{event_id}" in a.client.beta.messages.calls[0]["messages"][-1]["content"]
    [event] = s.events(1, datetime(2000, 1, 1, tzinfo=TZ))
    assert event.kind == NIGHT
    assert event.ts.astimezone(TZ) == resolve_time("21:30", "yesterday", datetime.now(TZ))
    assert "Исправил" in reply


def test_set_bedtime_and_settings():
    s = configured_storage()
    s.add_event(1, WAKE, datetime.now(TZ) - timedelta(hours=1))
    a = fake_assistant([
        ("tool_use", [tool_use("t1", "set_plan_time", target="bedtime", time="23:50"),
                      tool_use("t2", "update_settings", setting="remind_before_minutes", value="15")]),
        ("end_turn", [text("Отбой сегодня в 23:50, напомню за 15 минут.")]),
    ])
    reply = say(s, a, "сегодня в гостях, уложим в 23:50, и напоминай за 15 минут")
    assert "Отбой — 23:50 (назначено вами)" in reply
    assert s.chat(1).remind_before == 15


def test_free_question_without_changes():
    s = configured_storage()
    a = fake_assistant([("end_turn", [text("Обычно в 2 года хватает одной прогулки днём.")])])
    reply = say(s, a, "сколько гулять с ребёнком зимой?")
    assert reply == "Обычно в 2 года хватает одной прогулки днём."
    assert s.events(1, datetime(2000, 1, 1, tzinfo=TZ)) == []


def test_tool_error_goes_back_to_model():
    s = configured_storage()
    a = fake_assistant([
        ("tool_use", [tool_use("t1", "delete_event", event_id=999)]),
        ("end_turn", [text("Не нашёл такой отметки.")]),
    ])
    say(s, a, "удали отметку")
    result = a.client.beta.messages.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "#999" in result["content"]


def test_failure_after_changes_does_not_duplicate():
    s = configured_storage()
    a = fake_assistant([
        ("tool_use", [tool_use("t1", "record_event", kind="wake", time="now", day="today")]),
        ConnectionError("обрыв"),
    ])
    reply = say(s, a, "проснулся")
    assert "Изменения внесены" in reply
    assert [e.kind for e in s.events(1, datetime(2000, 1, 1, tzinfo=TZ))] == [WAKE]


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


def test_cache_breakpoints():
    s = configured_storage()
    a = fake_assistant([
        ("end_turn", [text("Привет!")]),
        ("end_turn", [text("Отбой около 19:15.")]),
    ])
    say(s, a, "привет")
    say(s, a, "когда отбой?")
    first, second = a.client.beta.messages.calls
    # Инструкции и инструменты — на час, одинаковые для всех запросов.
    assert first["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert first["system"] == second["system"]
    # Хвост запроса — автоматический кэш на 5 минут.
    assert first["cache_control"] == {"type": "ephemeral"}
    # Во втором сообщении метка стоит на конце истории, а сама история не изменилась.
    last_history = second["messages"][1]
    assert last_history["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert last_history["content"][0]["text"] == "Привет!"
    assert a.history[1][1] == {"role": "assistant", "content": "Привет!"}
