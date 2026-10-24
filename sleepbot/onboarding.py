"""Анкета при первом запуске: имя, пол, возраст, дневные сны, обычный подъём, часовой пояс.

На каждый вопрос можно ответить голосом или текстом. Если в одном ответе сказано
сразу несколько вещей («Маша, девочка, два года»), лишние вопросы пропускаются.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from .parsing import (
    birth_date_from_age, normalize, parse_age_months, parse_birth_date, parse_clock,
    parse_gender, parse_name, parse_naps,
)

STEPS = ("name", "gender", "age", "naps", "wake", "city", "localtime")

# Основа названия города → часовой пояс.
CITIES = {
    "москв": "Europe/Moscow", "петербург": "Europe/Moscow", "питер": "Europe/Moscow",
    "спб": "Europe/Moscow", "казан": "Europe/Moscow", "нижн": "Europe/Moscow",
    "ростов": "Europe/Moscow", "краснодар": "Europe/Moscow", "сочи": "Europe/Moscow",
    "воронеж": "Europe/Moscow", "ярослав": "Europe/Moscow", "тул": "Europe/Moscow",
    "крым": "Europe/Moscow", "симферопол": "Europe/Moscow", "севастопол": "Europe/Moscow",
    "минск": "Europe/Minsk", "калининград": "Europe/Kaliningrad",
    "самар": "Europe/Samara", "ижевск": "Europe/Samara", "волгоград": "Europe/Volgograd",
    "саратов": "Europe/Saratov", "екатеринбург": "Asia/Yekaterinburg", "челябинск": "Asia/Yekaterinburg",
    "пермь": "Asia/Yekaterinburg", "перми": "Asia/Yekaterinburg", "уф": "Asia/Yekaterinburg",
    "тюмен": "Asia/Yekaterinburg", "омск": "Asia/Omsk", "новосибирск": "Asia/Novosibirsk",
    "томск": "Asia/Tomsk", "барнаул": "Asia/Barnaul", "кемерово": "Asia/Novokuznetsk",
    "новокузнецк": "Asia/Novokuznetsk", "красноярск": "Asia/Krasnoyarsk", "иркутск": "Asia/Irkutsk",
    "якутск": "Asia/Yakutsk", "хабаровск": "Asia/Vladivostok", "владивосток": "Asia/Vladivostok",
    "магадан": "Asia/Magadan", "камчат": "Asia/Kamchatka",
    "киев": "Europe/Kyiv", "киів": "Europe/Kyiv", "алмат": "Asia/Almaty", "астан": "Asia/Almaty",
    "ташкент": "Asia/Tashkent", "бишкек": "Asia/Bishkek", "тбилиси": "Asia/Tbilisi",
    "ереван": "Asia/Yerevan", "баку": "Asia/Baku", "кишин": "Europe/Chisinau",
    "рига": "Europe/Riga", "вильн": "Europe/Vilnius", "таллин": "Europe/Tallinn",
    "берлин": "Europe/Berlin", "мюнхен": "Europe/Berlin", "прага": "Europe/Prague",
    "варшав": "Europe/Warsaw", "вен": "Europe/Vienna", "париж": "Europe/Paris",
    "лондон": "Europe/London", "мадрид": "Europe/Madrid", "барселон": "Europe/Madrid",
    "рим": "Europe/Rome", "милан": "Europe/Rome", "белград": "Europe/Belgrade",
    "стамбул": "Europe/Istanbul", "анталь": "Europe/Istanbul", "тель-авив": "Asia/Jerusalem",
    "израил": "Asia/Jerusalem", "хайф": "Asia/Jerusalem", "дубай": "Asia/Dubai",
    "бангкок": "Asia/Bangkok", "пхукет": "Asia/Bangkok", "бали": "Asia/Makassar",
    "лимасол": "Asia/Nicosia", "кипр": "Asia/Nicosia", "нью-йорк": "America/New_York",
}


@dataclass
class Question:
    text: str
    options: list[str] = field(default_factory=list)


def question(step: str, name: str | None, gender: str | None) -> Question:
    him = {"m": "ему", "f": "ей"}.get(gender, "ребёнку")
    he = {"m": "он", "f": "она"}.get(gender, "малыш")
    return {
        "name": Question("Как зовут ребёнка?"),
        "gender": Question(f"{name} — мальчик или девочка?", ["Мальчик", "Девочка"]),
        "age": Question(f"Сколько {him} лет? Можно назвать дату рождения — так точнее, "
                        "например: 12.03.2024"),
        "naps": Question(f"Сколько раз в день {he} сейчас спит днём?",
                         ["Не спит днём", "1 раз", "2 раза", "Не знаю"]),
        "wake": Question(f"Во сколько {he} обычно просыпается утром? Если по-разному — "
                         "так и скажите: день всё равно считаю от настоящего подъёма.",
                         ["6:30", "7:00", "7:30", "8:00", "По-разному"]),
        "city": Question("В каком городе вы живёте? Это нужно для часового пояса.",
                         ["Москва", "Санкт-Петербург", "Екатеринбург", "Новосибирск"]),
        "localtime": Question("Не знаю этот город. Сколько у вас сейчас времени? Например: 14:35"),
    }[step]


@dataclass
class Answer:
    updates: dict
    next_step: str | None  # None — анкета заполнена
    error: str | None = None


def city_tz(text: str) -> str | None:
    t = text.lower().replace("ё", "е")
    for stem, tz in CITIES.items():
        if re.search(rf"\b{re.escape(stem)}", t):
            return tz
    return None


def offset_tz(local: tuple[int, int], now_utc: datetime) -> str:
    """Часовой пояс по названному местному времени, с точностью до получаса."""
    hour, minute = local
    local_dt = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
    diff = (local_dt - now_utc).total_seconds() / 60
    diff = (diff + 12 * 60) % (24 * 60) - 12 * 60  # в диапазон −12…+12 ч
    minutes = round(diff / 30) * 30
    sign = "+" if minutes >= 0 else "-"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h:02d}:{m:02d}"


def _age(text: str, today: date) -> date | None:
    if birth := parse_birth_date(text):
        return birth if birth <= today else None
    months = parse_age_months(text)
    return birth_date_from_age(months, today) if months is not None else None


def apply(step: str, text: str, filled: dict, today: date, now_utc: datetime) -> Answer:
    """Разбирает ответ на вопрос step. filled — уже известные поля анкеты."""
    updates: dict = {}
    err = None
    if step == "name":
        name = parse_name(text)
        if not name or not re.match(r"[^\d\W]", name):
            return Answer({}, step, "Не расслышал имя. Напишите или скажите ещё раз.")
        updates["child_name"] = name
    elif step == "gender":
        if g := parse_gender(text):
            updates["gender"] = g
        else:
            err = "Мальчик или девочка?"
    elif step == "age":
        if birth := _age(text, today):
            updates["birth_date"] = birth
        else:
            err = "Не понял возраст. Например: «2 года», «год и 3 месяца» или 12.03.2024"
    elif step == "naps":
        if re.search(r"не знаю|по возрасту|как обычно", text, re.I):
            updates["naps_pref"] = None
        elif (n := parse_naps(text)) is not None and n <= 3:
            updates["naps_pref"] = n
        else:
            err = "Сколько раз днём? Например: «один раз» или «не спит днём»."
    elif step == "wake":
        clock = parse_clock(normalize(text)) or _bare_hour(text)
        if re.search(r"по.?разному|разн|когда как|не знаю", text, re.I):
            updates["wake_usual"] = None
        elif clock and 4 <= clock[0] <= 11:
            updates["wake_usual"] = f"{clock[0]:02d}:{clock[1]:02d}"
        else:
            err = "Во сколько обычно подъём? Например: 7:00"
    elif step == "city":
        if tz := city_tz(text):
            updates["tz"] = tz
            return Answer(updates, None)
        return Answer({}, "localtime")
    elif step == "localtime":
        clock = parse_clock(normalize(text)) or _bare_hour(text)
        if not clock:
            return Answer({}, step, "Назовите время, например: 14:35")
        updates["tz"] = offset_tz(clock, now_utc)
        return Answer(updates, None)
    if err:
        return Answer({}, step, err)

    # Дополнительно вытаскиваем то, что сказали заодно.
    known = {**filled, **updates}
    if not known.get("gender") and (g := parse_gender(text)):
        updates["gender"] = g
    if not known.get("birth_date") and step != "wake" and (birth := _age(text, today)):
        updates["birth_date"] = birth
    known.update(updates)

    order = STEPS[STEPS.index(step) + 1:]
    for nxt in order:
        if nxt == "gender" and known.get("gender"):
            continue
        if nxt == "age" and known.get("birth_date"):
            continue
        if nxt == "localtime":
            continue
        return Answer(updates, nxt)
    return Answer(updates, None)


def _bare_hour(text: str) -> tuple[int, int] | None:
    t = normalize(text)
    if m := re.search(r"(?<![\d:.])([0-2]?\d)(?:\s+([0-5]\d))?(?![\d:.])", t):
        hour = int(m.group(1))
        if hour < 24:
            return hour, int(m.group(2) or 0)
    return None

