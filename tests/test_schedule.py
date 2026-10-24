from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sleepbot.parsing import parse_birth_date, parse_event
from sleepbot.schedule import (
    NAP, NIGHT, WAKE, Event, age_in_months, build_plan, classify_sleep, due_reminders,
    format_age, norm_for_age,
)

TZ = ZoneInfo("Europe/Moscow")
NORM_2Y = norm_for_age(26)


def at(h, m=0, day=24):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


def ev(i, kind, ts):
    return Event(i, kind, ts)


def test_age():
    assert age_in_months(date(2024, 8, 10), date(2026, 10, 24)) == 26
    assert age_in_months(date(2024, 10, 25), date(2026, 10, 24)) == 23
    assert format_age(26) == "2 года 2 мес."
    assert len(norm_for_age(26).naps) == 1
    assert len(norm_for_age(10).naps) == 2


def test_two_year_old_day():
    events = [ev(1, NIGHT, at(20, 0, 23)), ev(2, WAKE, at(7, 0))]
    plan = build_plan(events, at(8), NORM_2Y)
    assert plan.status == "awake"
    assert plan.morning_wake.id == 2
    assert plan.naps[0].start == at(12, 30)
    assert plan.naps[0].end == at(14, 15)
    assert plan.bedtime == at(19, 15)


def test_bedtime_follows_real_nap():
    events = [ev(1, NIGHT, at(20, 0, 23)), ev(2, WAKE, at(7)), ev(3, NAP, at(13)), ev(4, WAKE, at(14))]
    plan = build_plan(events, at(15), NORM_2Y)
    assert plan.naps[0].finished
    assert plan.bedtime == at(19)


def test_skipped_nap_moves_bedtime_earlier():
    events = [ev(1, NIGHT, at(20, 0, 23)), ev(2, WAKE, at(7))]
    plan = build_plan(events, at(15), NORM_2Y)
    assert plan.naps[0].skipped
    assert plan.bedtime == at(17, 30)


def test_night_status_has_no_plan():
    plan = build_plan([ev(1, WAKE, at(7)), ev(2, NIGHT, at(20))], at(22), NORM_2Y)
    assert plan.status == "night" and plan.bedtime is None


def test_classify_sleep():
    events = [ev(1, NIGHT, at(20, 0, 23)), ev(2, WAKE, at(7))]
    assert classify_sleep(events, at(12, 40), NORM_2Y, 12) == NAP
    after_nap = events + [ev(3, NAP, at(12, 40)), ev(4, WAKE, at(14, 30))]
    assert classify_sleep(after_nap, at(17), NORM_2Y, 17) == NIGHT
    assert classify_sleep(events, at(20, 30), NORM_2Y, 20) == NIGHT


def test_reminders_before_and_at_nap():
    events = [ev(1, NIGHT, at(20, 0, 23)), ev(2, WAKE, at(7))]
    before = timedelta(minutes=30)

    def due(now):
        plan = build_plan(events, now, NORM_2Y)
        return [r.key for r in due_reminders(plan, now, TZ, NORM_2Y, before, None, "Миша")]

    assert due(at(11, 59)) == []
    assert due(at(12, 0)) == ["2:nap0@1230:pre"]
    assert due(at(12, 30)) == ["2:nap0@1230:now"]
    assert due(at(12, 50)) == []


def test_bedtime_reminder_after_nap():
    events = [ev(1, NIGHT, at(20, 0, 23)), ev(2, WAKE, at(7)), ev(3, NAP, at(13)), ev(4, WAKE, at(14))]
    now = at(18, 30)
    plan = build_plan(events, now, NORM_2Y)
    keys = [r.key for r in due_reminders(plan, now, TZ, NORM_2Y, timedelta(minutes=30), None, "Миша")]
    assert keys == ["2:bed@1900:pre"]


def test_long_nap_and_morning_check():
    events = [ev(1, NIGHT, at(20, 0, 23)), ev(2, WAKE, at(7)), ev(3, NAP, at(12))]
    now = at(14, 40)
    plan = build_plan(events, now, NORM_2Y)
    assert [r.key for r in due_reminders(plan, now, TZ, NORM_2Y, timedelta(0), None, "Миша")] == ["long:3"]

    night = build_plan([ev(1, NIGHT, at(20, 0, 23))], at(8, 31), NORM_2Y)
    keys = [r.key for r in due_reminders(night, at(8, 31), TZ, NORM_2Y, timedelta(0), at(8, 30), "Миша")]
    assert keys == ["morning:2026-10-24"]


def test_parse_event():
    now = at(10, 0)
    p = parse_event("Проснулся в 7:20", now)
    assert p.action == WAKE and p.at == at(7, 20)
    p = parse_event("уснула 15 минут назад", now)
    assert p.action == "sleep" and p.kind is None and p.at == at(9, 45)
    p = parse_event("легли на ночь в 21.30", at(0, 20))
    assert p.kind == NIGHT and p.at == at(21, 30, day=23)
    p = parse_event("проснулся после дневного сна", now)
    assert p.action == WAKE and p.at == now
    assert parse_event("😴 Уснули", now).action == "sleep"
    assert parse_event("☀️ Проснулись", now).action == WAKE
    assert parse_event("привет", now) is None


def test_parse_birth_date():
    assert parse_birth_date("Миша 12.03.2024") == date(2024, 3, 12)
    assert parse_birth_date("Миша") is None


def test_short_night_moves_day_earlier():
    # Норма в 2 года — от 10,5 ч; ночь 22:30–07:00 = 8,5 ч, недосып 2 ч.
    events = [ev(1, NIGHT, at(22, 30, 23)), ev(2, WAKE, at(7, 0))]
    plan = build_plan(events, at(8), NORM_2Y)
    assert plan.night == timedelta(hours=8, minutes=30)
    assert plan.nap_shift == timedelta(minutes=40)
    assert plan.bed_shift == timedelta(minutes=30)
    assert plan.naps[0].start == at(11, 50)  # вместо 12:30
    assert plan.bedtime == at(18, 5)  # 11:50 + 1:45 сна + 5 ч − 30 мин


def test_normal_night_keeps_day():
    events = [ev(1, NIGHT, at(20, 0, 23)), ev(2, WAKE, at(7, 0))]
    plan = build_plan(events, at(8), NORM_2Y)
    assert plan.night == timedelta(hours=11)
    assert plan.nap_shift == plan.bed_shift == timedelta(0)


def test_manual_times_override_plan():
    events = [ev(1, NIGHT, at(20, 0, 23)), ev(2, WAKE, at(7))]
    plan = build_plan(events, at(8), NORM_2Y, {(2, "nap0"): at(13, 0), (2, "bed"): at(20, 0)})
    assert plan.naps[0].start == at(13, 0) and plan.bedtime == at(20, 0)
    assert plan.manual == {"nap0", "bed"}
    # Время на другой день не применяется.
    assert build_plan(events, at(8), NORM_2Y, {(99, "bed"): at(20, 0)}).bedtime == at(19, 15)
