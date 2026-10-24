"""Разбор свободного текста и расшифровок голосовых: «проснулся в семь двадцать»,
«уснула 15 минут назад», «когда нам укладываться?», ответы анкеты."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .schedule import NAP, NIGHT, WAKE

_WAKE = re.compile(r"просну|проснё|встал|встаём|встаем|подъ[её]м|разбуд", re.I)
_SLEEP = re.compile(r"усну|засну|засып|л[её]г\b|легл|легли|спит|спим|отбой|уложил", re.I)
_NIGHT = re.compile(r"ноч|отбой", re.I)
_NAP = re.compile(r"дн[её]в|днём|днем", re.I)
_QUESTION = re.compile(r"\?|когда|во сколько|сколько|какой план|что по плану|план\b|расписани", re.I)
_PAST_SLEEP = re.compile(r"сколько\s+(?:он\s+|она\s+|мы\s+)?(?:спал|поспал|спали)", re.I)

_CLOCK = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.](\d{2})(?!\d)")
_HOURS_MINUTES = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*час\w*\s*(\d{1,2})\s*мин")
_SPACED = re.compile(r"\bв\s+([01]?\d|2[0-3])\s+([0-5]\d)(?!\s*(?:мин|ч))")
_HOUR_ONLY = re.compile(r"\bв\s+([01]?\d|2[0-3])(?!\s*(?:мин|[:.\d]))")
_AGO = re.compile(r"(\d+)\s*(мин|м\b|ч|час)\w*\s+назад", re.I)

# --- числа словами -----------------------------------------------------------------

_UNITS = {
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
}
_TEENS = {
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19,
}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50}
# «половина восьмого», «без пятнадцати девять» — часы в родительном / именительном падеже.
_ORDINAL_GEN = {
    "первого": 1, "второго": 2, "третьего": 3, "четвёртого": 4, "четвертого": 4,
    "пятого": 5, "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9,
    "десятого": 10, "одиннадцатого": 11, "двенадцатого": 12,
}
_GENITIVE = {
    "одной": 1, "двух": 2, "трёх": 3, "трех": 3, "четырёх": 4, "четырех": 4, "пяти": 5,
    "десяти": 10, "пятнадцати": 15, "двадцати": 20,
}


def _words_to_digits(text: str) -> str:
    tokens = re.findall(r"\w+|\W+", text)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        w = tokens[i].lower()
        if w in _TENS:
            value = _TENS[w]
            # «двадцать пять»: десяток + пробел + единица
            if i + 2 < len(tokens) and tokens[i + 1].isspace() and tokens[i + 2].lower() in _UNITS:
                value += _UNITS[tokens[i + 2].lower()]
                i += 2
            out.append(str(value))
        elif w in _TEENS:
            out.append(str(_TEENS[w]))
        elif w in _UNITS:
            out.append(str(_UNITS[w]))
        else:
            out.append(tokens[i])
        i += 1
    return "".join(out)


def normalize(text: str) -> str:
    """Числа словами → цифры, разговорные формы времени → ЧЧ:ММ."""
    t = text.lower()
    t = re.sub(r"\bпол\s*-?\s*(\w+ого)\b", lambda m: f"{_ORDINAL_GEN[m.group(1)] - 1}:30"
               if m.group(1) in _ORDINAL_GEN else m.group(0), t)  # «полвосьмого»
    t = re.sub(r"\bполови\w*\s+(\w+)", lambda m: f"{_ORDINAL_GEN[m.group(1)] - 1}:30"
               if m.group(1) in _ORDINAL_GEN else m.group(0), t)

    def without(m: re.Match) -> str:
        minutes = _GENITIVE.get(m.group(1))
        hour_word = m.group(2)
        hour = _UNITS.get(hour_word) or _TEENS.get(hour_word)
        if hour_word.isdigit():
            hour = int(hour_word)
        if not minutes or not hour:
            return m.group(0)
        return f"{hour - 1}:{60 - minutes:02d}"

    t = re.sub(r"\bбез\s+(\w+)\s+(\w+)", without, t)
    t = _words_to_digits(t)
    t = re.sub(r"\bполчаса\b", "30 минут", t)
    t = re.sub(r"(?<!\d\s)\bчас назад", "1 час назад", t)
    return t


# --- события и вопросы ---------------------------------------------------------------


@dataclass(frozen=True)
class Parsed:
    action: str  # WAKE или "sleep"
    kind: str | None  # NAP/NIGHT, если указано явно
    at: datetime


def parse_clock(text: str) -> tuple[int, int] | None:
    """Время суток из уже нормализованного текста."""
    for pattern in (_CLOCK, _HOURS_MINUTES, _SPACED):
        if m := pattern.search(text):
            return int(m.group(1)), int(m.group(2))
    if m := _HOUR_ONLY.search(text):
        return int(m.group(1)), 0
    return None


def parse_time(text: str, now: datetime) -> datetime:
    """Время события в часовом поясе now. Без времени — сейчас."""
    t = normalize(text)
    if m := _AGO.search(t):
        n = int(m.group(1))
        return now - (timedelta(hours=n) if m.group(2).startswith("ч") else timedelta(minutes=n))
    clock = parse_clock(t)
    if clock is None:
        return now
    hour, minute = clock
    if hour < 12 and re.search(r"вечера|дня\b", t):
        hour += 12  # «в 8 вечера», «в 2 дня»
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at > now + timedelta(minutes=5):
        at -= timedelta(days=1)  # «уснул в 21:30», сказанное после полуночи
    return at


def is_question(text: str) -> bool:
    return bool(_QUESTION.search(text))


def question_topic(text: str) -> str:
    """О чём спрашивают: "history" — сколько спал, "night" — отбой, "nap" — дневной, "next" — ближайшее."""
    if _PAST_SLEEP.search(text):
        return "history"
    if _NIGHT.search(text) or re.search(r"вечер|на ночь", text, re.I):
        return "night"
    if _NAP.search(text):
        return "nap"
    return "next"


def parse_event(text: str, now: datetime) -> Parsed | None:
    wake = _WAKE.search(text)
    sleep = _SLEEP.search(text)
    if not wake and not sleep:
        return None
    # «проснулся после дневного сна» — это подъём, хотя дальше есть слово про сон.
    action = WAKE if wake and (not sleep or wake.start() < sleep.start()) else "sleep"
    kind = None
    if action == "sleep":
        if _NIGHT.search(text):
            kind = NIGHT
        elif _NAP.search(text):
            kind = NAP
    return Parsed(action, kind, parse_time(text, now))


# --- анкета -------------------------------------------------------------------------


def parse_birth_date(text: str) -> date | None:
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def parse_age_months(text: str) -> int | None:
    """«2 года», «два с половиной года», «полтора», «год и три месяца», «10 месяцев»."""
    t = normalize(text)
    if "полтор" in t:
        return 18
    years = 0
    months = 0
    found = False
    if m := re.search(r"(\d+)\s*(?:с половиной\s*)?(?:год|года|лет)\b", t):
        years = int(m.group(1))
        found = True
        if "с половиной" in t:
            months += 6
    elif re.search(r"\bгод\b|\bгодик", t):
        years = 1
        found = True
    if m := re.search(r"(\d+)\s*(?:мес|месяц)", t):
        months += int(m.group(1))
        found = True
    if not found:
        if m := re.fullmatch(r"\s*(\d{1,2})\s*", t):
            return int(m.group(1)) * 12  # просто «2» в ответ на вопрос о возрасте
        return None
    total = years * 12 + months
    return total if 0 < total < 12 * 10 else None


def birth_date_from_age(months: int, today: date) -> date:
    year, month = divmod(today.year * 12 + today.month - 1 - months, 12)
    day = min(today.day, 28)
    return date(year, month + 1, day)


def parse_gender(text: str) -> str | None:
    t = text.lower()
    if re.search(r"девоч|дочк|дочь|доча|\bона\b|\bе[её]\s+зовут|женск", t):
        return "f"
    if re.search(r"мальч|сын|сынок|\bон\b|\bего\s+зовут|мужск|пацан", t):
        return "m"
    return None


def parse_naps(text: str) -> int | None:
    t = normalize(text)
    if re.search(r"не спит|не спим|нет|без дневного|\b0\b", t):
        return 0
    if m := re.search(r"\b([1-4])\b", t):
        return int(m.group(1))
    if re.search(r"\bраз\b|однажды|один", t):
        return 1
    return None


def parse_name(text: str) -> str:
    t = text.strip().strip(".!")
    t = re.sub(r"^(?:его|её|ее|нашего|нашу|нас|ребенка|ребёнка)?\s*(?:зовут|имя|это)\s+", "", t, flags=re.I)
    for _ in range(2):  # «наш сын Миша»
        t = re.sub(r"^(?:мы|наш[аеи]?|сын|дочь|дочка|сынок)\s+", "", t, flags=re.I)
    name = t.split()[0] if t.split() else ""
    name = re.sub(r"[^\w-]", "", name)
    return name[:1].upper() + name[1:] if name else ""
