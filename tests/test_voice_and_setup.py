"""Фразы, как их выдаёт распознавание речи, анкета и сквозной диалог с ботом без Telegram."""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from sleepbot import bot, onboarding
from sleepbot.parsing import (
    is_question, normalize, parse_age_months, parse_event, parse_gender, parse_name, parse_naps,
    question_topic,
)
from sleepbot.schedule import NAP, NIGHT, WAKE, norm_for
from sleepbot.storage import Storage

TZ = ZoneInfo("Europe/Moscow")


def at(h, m=0, day=24):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


@pytest.mark.parametrize("text, expected", [
    ("Проснулся в семь двадцать", at(7, 20)),
    ("проснулась в половине восьмого", at(7, 30)),
    ("встал без пятнадцати восемь", at(7, 45)),
    ("проснулся в пол восьмого", at(7, 30)),
    ("Проснулся в 7 часов 5 минут", at(7, 5)),
    ("Уснул двадцать минут назад", at(9, 40)),
    ("уснула полчаса назад", at(9, 30)),
    ("Мы проснулись час назад", at(9, 0)),
    ("Проснулся.", at(10, 0)),
])
def test_spoken_times(text, expected):
    assert parse_event(text, at(10)).at == expected


def test_spoken_evening():
    p = parse_event("легли спать в девять вечера", at(23, 0))
    assert p.action == "sleep" and p.kind is None and p.at == at(21, 0)


def test_normalize():
    assert normalize("двадцать пять минут") == "25 минут"
    assert normalize("в половине первого") == "в 0:30"


@pytest.mark.parametrize("text, topic", [
    ("Когда ему засыпать?", "next"),
    ("во сколько укладывать на ночь", "night"),
    ("Когда дневной сон?", "nap"),
    ("сколько он спал сегодня", "history"),
])
def test_questions(text, topic):
    assert is_question(text)
    assert question_topic(text) == topic


def test_statements_are_not_questions():
    assert not is_question("Проснулся в 7:20")
    assert not is_question("уснула")


def test_setup_parsers():
    assert parse_age_months("Ему два года") == 24
    assert parse_age_months("два с половиной года") == 30
    assert parse_age_months("год и три месяца") == 15
    assert parse_age_months("полтора") == 18
    assert parse_age_months("10 месяцев") == 10
    assert parse_gender("Мальчик") == "m"
    assert parse_gender("у нас девочка") == "f"
    assert parse_gender("Её зовут Аня") == "f"
    assert parse_naps("один раз") == 1
    assert parse_naps("Не спит днём") == 0
    assert parse_naps("два раза") == 2
    assert parse_name("Его зовут Миша.") == "Миша"
    assert parse_name("наш сын Тимофей") == "Тимофей"


def test_norm_follows_parent_nap_count():
    assert len(norm_for(26).naps) == 1
    assert len(norm_for(26, 0).naps) == 0
    assert len(norm_for(26, 2).naps) == 2


def test_onboarding_skips_answered_questions():
    today = date(2026, 10, 24)
    now = datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc)
    a = onboarding.apply("name", "Маша, девочка, два года", {}, today, now)
    assert a.updates["child_name"] == "Маша"
    assert a.updates["gender"] == "f"
    assert a.updates["birth_date"] == date(2024, 10, 24)
    assert a.next_step == "naps"
    assert onboarding.apply("city", "Новосибирск", {}, today, now).updates == {"tz": "Asia/Novosibirsk"}
    assert onboarding.apply("city", "Тмутаракань", {}, today, now).next_step == "localtime"
    local = onboarding.apply("localtime", "четырнадцать тридцать пять", {}, today, now)
    assert local.updates == {"tz": "UTC+07:30"} and local.next_step is None
    assert onboarding.apply("wake", "в семь утра", {}, today, now).updates == {"wake_usual": "07:00"}
    assert onboarding.apply("wake", "полвосьмого", {}, today, now).updates == {"wake_usual": "07:30"}
    assert onboarding.apply("wake", "не помню", {}, today, now).error  # переспросим
    assert onboarding.apply("wake", "По-разному.", {}, today, now).updates == {"wake_usual": None}


# --- сквозной диалог -------------------------------------------------------------------


class FakeMessage:
    def __init__(self, text, sent):
        self.text = text
        self.voice = None
        self.audio = None
        self.sent = sent

    async def reply_text(self, text, reply_markup=None):
        self.sent.append(text)


def say(storage, text, voice=False, chat_id=1):
    sent: list[str] = []
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type="private"),
        effective_message=FakeMessage(text, sent),
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"storage": storage}))
    asyncio.run(bot.handle_text(update, context, text, voice=voice))
    return "\n".join(sent)


def test_dialog_from_setup_to_question():
    s = Storage(":memory:")
    assert "Как зовут" in say(s, "привет")
    assert "мальчик или девочка" in say(s, "Миша", voice=True)
    assert "Сколько ему лет" in say(s, "мальчик")
    assert "спит днём" in say(s, "два года и два месяца")
    assert "просыпается" in say(s, "один раз")
    assert "городе" in say(s, "в семь")
    done = say(s, "Москва")
    assert "Готово! Миша, мальчик, 2 года 2 мес." in done
    assert "Отбой — 19:15" in done
    chat = s.chat(1)
    assert chat.configured and chat.tz == "Europe/Moscow" and chat.morning_check == "08:00"

    reply = say(s, "Проснулся", voice=True)
    assert reply.startswith("🎤 «Проснулся»")
    assert "Миша проснулся в" in reply
    assert [e.kind for e in s.events(1, at(0, 0, day=1))] == [WAKE]

    answer = say(s, "Когда ему засыпать?", voice=True)
    assert "Дневной сон в" in answer
    assert [e.kind for e in s.events(1, at(0, 0, day=1))] == [WAKE]  # вопрос не стал отметкой

    say(s, "уснул")
    assert s.last_event(1).kind in (NAP, NIGHT)


def test_dialog_wake_varies_and_night_answer():
    s = Storage(":memory:")
    for answer in ("привет", "Лёва", "мальчик", "2 года", "один раз"):
        say(s, answer)
    assert "городе" in say(s, "По-разному.", voice=True)
    assert s.chat(1).wake_usual is None and s.chat(1).morning_check == "09:00"
    say(s, "Москва")
    assert s.chat(1).configured

    reply = say(s, "проснулся в 7:10")
    assert "Не знаю, во сколько он уснул вчера" in reply
    reply = say(s, "в 21:15", voice=True)
    assert "Записал: уснул вчера в 21:15" in reply
    assert "Ночью спал 9 ч 55 мин" in reply
    assert "отбой на 10 мин раньше" in reply  # про дневной сон — только если он ещё впереди
    kinds = [e.kind for e in s.events(1, at(0, 0, day=1))]
    assert kinds == [NIGHT, WAKE]
