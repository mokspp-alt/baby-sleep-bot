"""Личный режим: отзывы о снах → недельный разбор → поправка окон после согласия."""

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sleepbot import bot
from sleepbot.learning import Sample, personal_norm, review, samples, verdict
from sleepbot.schedule import NAP, NIGHT, WAKE, Event, norm_for_age
from sleepbot.storage import Storage

TZ = ZoneInfo("Europe/Moscow")
NORM = norm_for_age(23)  # 1 год 11 мес.: окна 5 ч 15 мин и 4 ч 45 мин


def at(day, h, m=0):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


def night(day, latency=None, mood=None, wakings=None):
    return Sample(NIGHT, at(day, 20), timedelta(hours=5), latency, mood, wakings)


def test_personal_norm():
    norm = personal_norm(NORM, 15, -30)
    assert norm.wake_windows == (5.5, 4.25)
    assert personal_norm(NORM, 0, 0) is NORM


def test_verdicts():
    assert verdict(night(1, latency=40)) == "early"
    assert verdict(night(1, latency=5, wakings=3)) == "late"
    assert verdict(night(1, latency=5, mood="bad")) == "late"
    assert verdict(night(1, latency=15, mood="good", wakings=0)) == "good"
    assert verdict(night(1, mood="good")) == "good"
    assert verdict(night(1, latency=15, mood="bad")) is None  # непонятно — не считаем
    assert verdict(night(1)) is None


def test_review_suggests_later_bedtime():
    items = [night(d, latency=40) for d in (18, 19, 20, 21)] + [night(22, latency=15, mood="good")]
    result = review(items, at(24, 10), NORM, 0, 0, "Лёва", "m")
    [p] = result.proposals
    assert (p.target, p.delta) == ("bed", 15)
    assert "в 4 из 5 случаев Лёва засыпал дольше 30 минут" in p.text
    assert "бодрствование 5 ч вместо 4 ч 45 мин" in p.text


def test_review_suggests_earlier_when_overtired_and_respects_limit():
    items = [night(d, latency=5, wakings=2) for d in (18, 19, 20, 21)]
    [p] = review(items, at(24, 10), NORM, 0, 0, "Маша", "f").proposals
    assert (p.target, p.delta) == ("bed", -15) and "переутомление" in p.text

    capped = review(items, at(24, 10), NORM, 0, -90, "Маша", "f")
    assert capped.proposals == [] and "предельная" in capped.summary[0]


def test_review_confirms_good_schedule_and_needs_enough_data():
    good = [night(d, latency=10, mood="good", wakings=0) for d in (18, 19, 20, 21)]
    result = review(good, at(24, 10), NORM, 0, 0, "Лёва", "m")
    assert result.proposals == [] and "подходит" in result.summary[0]
    assert review(good[:3], at(24, 10), NORM, 0, 0, "Лёва", "m") == review([], at(24, 10), NORM, 0, 0,
                                                                          "Лёва", "m")


def test_samples_skip_unusual_days_and_need_wake_before():
    events = [Event(1, WAKE, at(20, 7)), Event(2, NAP, at(20, 12, 30)), Event(3, WAKE, at(20, 14)),
              Event(4, NIGHT, at(20, 19)), Event(5, NAP, at(21, 13))]
    feedback = {2: {"latency": 15}, 4: {"latency": 40}, 5: {"latency": 10}}
    got = samples(events, feedback, set(), TZ)
    assert [(s.kind, s.awake) for s in got] == [(NAP, timedelta(hours=5, minutes=30)),
                                                (NIGHT, timedelta(hours=5))]
    assert [s.kind for s in samples(events, feedback, {date(2026, 10, 20)}, TZ)] == []


def week_of_hard_bedtimes(s):
    """8 дней: подъём 7:00, сон 12:15–14:15, отбой 19:00; засыпает вечером по 40 минут."""
    for day in range(16, 24):
        s.add_event(1, WAKE, at(day, 7))
        s.add_event(1, NAP, at(day, 12, 15))
        s.add_event(1, WAKE, at(day, 14, 15))
        sleep_id = s.add_event(1, NIGHT, at(day, 19))
        s.set_feedback(1, sleep_id, latency=40, mood="good", wakings=0)


def test_weekly_review_and_apply():
    s = Storage(":memory:")
    s.update_chat(1, child_name="Лёва", gender="m", birth_date=date(2024, 11, 20))
    week_of_hard_bedtimes(s)
    chat = s.chat(1)
    text, ask = bot.weekly_review(chat, s, at(24, 10))
    assert ask and "Предлагаю отбой на 15 мин позже" in text
    assert bot.weekly_review(s.chat(1), s, at(25, 10)) is None  # следующий разбор — через неделю

    s.add_event(1, WAKE, at(24, 7))
    before = bot.day_plan(s.chat(1), s, at(24, 15)).bedtime
    assert "Применил" in bot.answer_review(s, s.chat(1), accept=True)
    assert s.chat(1).adj_bed == 15 and s.chat(1).pending is None
    assert bot.day_plan(s.chat(1), s, at(24, 15)).bedtime == before + timedelta(minutes=15)
    assert "неактуально" in bot.answer_review(s, s.chat(1), accept=True)


def test_review_waits_for_a_week_of_feedback():
    s = Storage(":memory:")
    s.update_chat(1, child_name="Лёва", gender="m", birth_date=date(2024, 11, 20))
    week_of_hard_bedtimes(s)
    assert bot.weekly_review(s.chat(1), s, at(21, 10)) is None  # отзывам меньше недели


def test_feedback_buttons():
    s = Storage(":memory:")
    s.update_chat(1, child_name="Лёва", gender="m", birth_date=date(2024, 11, 20))
    sleep_id = s.add_event(1, NIGHT, at(23, 20))
    wake = s.event(1, s.add_event(1, WAKE, at(24, 7)))

    labels = [[b.text for b in row] for row in bot.event_keyboard(wake, s, 1).inline_keyboard]
    assert labels[0] == ["😊", "😐", "😫"] and labels[1] == ["Ночью: 0", "1", "2+"]

    answers, markups = [], []

    async def answer(text=None):
        answers.append(text)

    async def edit_message_reply_markup(markup):
        markups.append(markup)

    query = SimpleNamespace(data=f"fb:{wake.id}:mood=bad", answer=answer,
                            edit_message_reply_markup=edit_message_reply_markup)
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=1))
    asyncio.run(bot.on_button(update, SimpleNamespace(application=SimpleNamespace(
        bot_data={"storage": s}))))
    assert s.feedback(1)[sleep_id]["mood"] == "bad"
    assert markups[0].inline_keyboard[0][2].text == "✓ 😫"

    sleep = s.event(1, sleep_id)
    labels = [b.text for b in bot.event_keyboard(sleep, s, 1).inline_keyboard[0]]
    assert labels == ["Уснул за ≤10 мин", "10–30", "30+ мин"]
