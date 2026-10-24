"""Разбор свободного текста: «проснулся в 7:20», «уснула 15 минут назад», «легли на ночь»."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .schedule import NAP, NIGHT, WAKE

_WAKE = re.compile(r"просну|проснё|встал|встаём|встаем|подъ[её]м|разбуд|разбудил", re.I)
_SLEEP = re.compile(r"усну|засну|засып|л[её]г|легл|легли|спит|спим|сон\b|отбой|уложил", re.I)
_NIGHT = re.compile(r"ноч|отбой", re.I)
_NAP = re.compile(r"дн[её]в|днём|днем", re.I)

_CLOCK = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.](\d{2})(?!\d)")
_HOUR_ONLY = re.compile(r"\bв\s+([01]?\d|2[0-3])(?!\s*(?:мин|ч|[:.\d]))")
_AGO = re.compile(r"(\d+)\s*(мин|м\b|ч|час)\w*\s+назад", re.I)


@dataclass(frozen=True)
class Parsed:
    action: str  # WAKE или "sleep"
    kind: str | None  # NAP/NIGHT, если указано явно
    at: datetime


def parse_time(text: str, now: datetime) -> datetime:
    """Время события в часовом поясе now. Без времени — сейчас."""
    t = text.lower()
    if "полчаса назад" in t:
        return now - timedelta(minutes=30)
    if re.search(r"(?<!\d\s)\bчас назад", t):
        return now - timedelta(hours=1)
    if m := _AGO.search(t):
        n = int(m.group(1))
        return now - (timedelta(hours=n) if m.group(2).startswith("ч") else timedelta(minutes=n))
    m = _CLOCK.search(t)
    hour, minute = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    if m is None and (m2 := _HOUR_ONLY.search(t)):
        hour, minute = int(m2.group(1)), 0
    if hour is None:
        return now
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at > now + timedelta(minutes=5):
        at -= timedelta(days=1)  # «уснул в 21:30», сказанное после полуночи
    return at


def parse_event(text: str, now: datetime) -> Parsed | None:
    wake = _WAKE.search(text)
    sleep = _SLEEP.search(text)
    if not wake and not sleep:
        return None
    # «проснулся после дневного сна» — это подъём, хотя в тексте есть «сон».
    action = WAKE if wake and (not sleep or wake.start() < sleep.start()) else "sleep"
    kind = None
    if action == "sleep":
        if _NIGHT.search(text):
            kind = NIGHT
        elif _NAP.search(text):
            kind = NAP
    return Parsed(action, kind, parse_time(text, now))


def parse_birth_date(text: str) -> date | None:
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
