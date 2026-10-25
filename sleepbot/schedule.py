"""Нормы сна по возрасту и расчёт плана дня. Чистая логика без Telegram и БД."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

WAKE = "wake"
NAP = "nap"
NIGHT = "night"


@dataclass(frozen=True)
class AgeNorm:
    """Окна бодрствования (часы) и длительность дневных снов (часы).

    wake_windows всегда на одно длиннее naps: окно перед каждым сном и окно перед ночью.
    """

    min_months: int
    wake_windows: tuple[float, ...]
    naps: tuple[float, ...]
    nap_max: float  # после этого дневной сон лучше прервать, чтобы не сбить ночь
    night_min: float  # нормальная длина ночного сна, часы
    night_max: float

    def __post_init__(self) -> None:
        assert len(self.wake_windows) == len(self.naps) + 1

    @property
    def night_hours(self) -> str:
        return f"{self.night_min:g}–{self.night_max:g}".replace(".", ",")


# Усреднённые рекомендации педиатров и сомнологов. У каждого ребёнка свой ритм:
# это стартовая точка, а не правило.
NORMS: tuple[AgeNorm, ...] = (
    AgeNorm(4, (1.75, 2.0, 2.0, 2.25), (1.25, 1.25, 0.5), 2.0, 11, 12),
    AgeNorm(6, (2.5, 2.75, 3.0), (1.5, 1.25), 2.0, 11, 12),
    AgeNorm(9, (3.0, 3.25, 3.75), (1.5, 1.25), 2.0, 11, 12),
    AgeNorm(12, (3.25, 3.75, 4.0), (1.5, 1.0), 2.0, 11, 12),
    AgeNorm(15, (5.0, 4.5), (2.25,), 3.0, 11, 12),
    AgeNorm(18, (5.25, 4.75), (2.0,), 2.75, 11, 12),
    AgeNorm(24, (5.5, 5.0), (1.75,), 2.5, 10.5, 11.5),
    AgeNorm(36, (6.0, 5.5), (1.25,), 1.75, 10.5, 11.5),
    AgeNorm(48, (12.5,), (), 0.0, 10, 11),
)


def age_in_months(birth: date, today: date) -> int:
    months = (today.year - birth.year) * 12 + today.month - birth.month
    if today.day < birth.day:
        months -= 1
    return max(months, 0)


def norm_for_age(months: int) -> AgeNorm:
    chosen = NORMS[0]
    for norm in NORMS:
        if months >= norm.min_months:
            chosen = norm
    return chosen


def norm_for(months: int, naps: int | None = None) -> AgeNorm:
    """Норма по возрасту; если родители сказали, сколько раз ребёнок спит днём,
    берём ближайшую по возрасту норму с таким числом снов."""
    base = norm_for_age(months)
    if naps is None or naps == len(base.naps):
        return base
    same = [n for n in NORMS if len(n.naps) == naps]
    return min(same, key=lambda n: abs(n.min_months - months)) if same else base


def gendered(gender: str | None, masc: str, fem: str) -> str:
    if gender == "f":
        return fem
    if gender == "m":
        return masc
    return f"{masc}/{fem}"


def format_age(months: int) -> str:
    years, rest = divmod(months, 12)
    if years == 0:
        return f"{rest} мес."
    word = "год" if years == 1 else ("года" if years < 5 else "лет")
    return f"{years} {word}" + (f" {rest} мес." if rest else "")


@dataclass(frozen=True)
class Event:
    id: int
    kind: str  # WAKE, NAP или NIGHT
    ts: datetime  # aware


@dataclass
class PlannedNap:
    start: datetime
    end: datetime
    started: bool = False  # начало подтверждено событием
    finished: bool = False  # конец подтверждён событием
    skipped: bool = False
    event_id: int | None = None


@dataclass
class DayPlan:
    status: str  # "awake", "nap", "night", "unknown"
    status_since: datetime | None
    morning_wake: Event | None = None
    naps: list[PlannedNap] = field(default_factory=list)
    bedtime: datetime | None = None
    night: timedelta | None = None  # сколько спал прошлой ночью
    nap_shift: timedelta = timedelta(0)  # насколько раньше дневной сон из-за короткой ночи
    bed_shift: timedelta = timedelta(0)  # насколько раньше отбой из-за короткой ночи
    manual: set[str] = field(default_factory=set)  # "nap0", "bed" — назначены родителями


# Если запланированный сон не начался спустя столько времени — считаем его пропущенным.
SKIP_AFTER = timedelta(hours=2)


def _hours(h: float) -> timedelta:
    return timedelta(minutes=round(h * 60))


Overrides = dict[tuple[int, str], datetime]


def build_plan(events: list[Event], now: datetime, norm: AgeNorm,
               overrides: Overrides | None = None) -> DayPlan:
    """Строит план текущего дня по событиям (в хронологическом порядке).

    overrides — время, назначенное родителями вручную: {(id утреннего подъёма, "nap0" / "bed"): время}.
    """
    events = sorted(events, key=lambda e: e.ts)
    if not events:
        return DayPlan("unknown", None)

    last = events[-1]
    status = {WAKE: "awake", NAP: "nap", NIGHT: "night"}[last.kind]
    plan = DayPlan(status, last.ts)
    if status == "night":
        return plan

    # Утренний подъём — первый подъём после последнего ночного сна,
    # а без ночного сна в истории — первый подъём за последние сутки.
    night_idx = max((i for i, e in enumerate(events) if e.kind == NIGHT), default=-1)
    candidates = events[night_idx + 1:]
    if night_idx < 0:
        candidates = [e for e in candidates if now - e.ts < timedelta(hours=20)]
    first_wake = next((i for i, e in enumerate(candidates) if e.kind == WAKE), None)
    if first_wake is None:
        return plan
    day = candidates[first_wake:]
    plan.morning_wake = day[0]

    actual: list[PlannedNap] = []
    for i, e in enumerate(day):
        if e.kind != NAP:
            continue
        wake = next((w for w in day[i + 1:] if w.kind == WAKE), None)
        actual.append(PlannedNap(e.ts, wake.ts if wake else e.ts, True, wake is not None, event_id=e.id))

    if night_idx >= 0 and events[night_idx].ts < plan.morning_wake.ts:
        night = plan.morning_wake.ts - events[night_idx].ts
        if timedelta(hours=3) <= night <= timedelta(hours=16):
            plan.night = night
            plan.nap_shift, plan.bed_shift = night_shifts(night, norm)

    cursor = plan.morning_wake.ts
    for i in range(max(len(norm.naps), len(actual))):
        window = _hours(norm.wake_windows[min(i, len(norm.wake_windows) - 1)])
        if i == 0:
            window -= plan.nap_shift
        duration = _hours(norm.naps[min(i, len(norm.naps) - 1)]) if norm.naps else timedelta(hours=1)
        if i < len(actual):
            nap = actual[i]
            if not nap.finished:
                nap.end = max(nap.start + duration, now)
        else:
            start = cursor + window
            if manual := (overrides or {}).get((plan.morning_wake.id, f"nap{i}")):
                start = manual
                plan.manual.add(f"nap{i}")
            nap = PlannedNap(start, start + duration)
            if now > start + SKIP_AFTER:
                nap.skipped = True
                nap.end = start  # пропущенный сон: следующее окно отсчитываем от его плана
        plan.naps.append(nap)
        cursor = nap.end

    plan.bedtime = cursor + _hours(norm.wake_windows[-1]) - plan.bed_shift
    if manual := (overrides or {}).get((plan.morning_wake.id, "bed")):
        plan.bedtime = manual
        plan.manual.add("bed")
    return plan


def _round5(minutes: float) -> timedelta:
    return timedelta(minutes=5 * round(minutes / 5))


def night_shifts(night: timedelta, norm: AgeNorm) -> tuple[timedelta, timedelta]:
    """Насколько раньше (плюс) или позже (минус) сдвинуть дневной сон и отбой из-за ночи.

    Недоспал — устаёт раньше: за каждый час недосыпа дневной сон на 20 мин раньше
    (не больше часа), отбой на 15 мин раньше (не больше 45 мин).
    Проспал больше нормы — может бодрствовать чуть дольше, но эффект слабее: за каждый
    час сверх нормы оба сдвигаются на 15 мин позже, не больше чем на полчаса.
    Разницу меньше 15 минут не считаем. Если дневного сна нет, сдвигаем только отбой.
    """
    hours = night.total_seconds() / 3600
    deficit = norm.night_min - hours
    surplus = hours - norm.night_max
    if deficit >= 0.25:
        nap, bed = _round5(min(deficit * 20, 60)), _round5(min(deficit * 15, 45))
    elif surplus >= 0.25:
        nap = bed = -_round5(min(surplus * 15, 30))
    else:
        return timedelta(0), timedelta(0)
    return (nap if norm.naps else timedelta(0)), bed


def classify_sleep(events: list[Event], at: datetime, norm: AgeNorm, local_hour: int) -> str:
    """Решает, дневной это сон или ночной, если родитель не сказал явно."""
    if local_hour >= 18 or local_hour < 5:
        return NIGHT
    plan = build_plan([e for e in events if e.ts <= at], at, norm)
    if plan.morning_wake is None:
        return NAP
    naps_done = sum(1 for n in plan.naps if n.started)
    if naps_done >= len(norm.naps):
        return NIGHT
    if plan.bedtime and at >= plan.bedtime - timedelta(hours=1, minutes=30):
        return NIGHT
    return NAP


@dataclass(frozen=True)
class Reminder:
    key: str
    text: str


PRE_WINDOW = timedelta(minutes=10)
AT_WINDOW = timedelta(minutes=15)


def _fmt(dt: datetime, tz) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def due_reminders(
    plan: DayPlan,
    now: datetime,
    tz,
    norm: AgeNorm,
    before: timedelta,
    morning_check: datetime | None,
    name: str,
    gender: str | None = None,
) -> list[Reminder]:
    """Напоминания, которые пора отправить сейчас. Повторы отсекаются по ключу снаружи.

    Окна короткие, чтобы после перезапуска бот не присылал давно устаревшее.
    """
    out: list[Reminder] = []

    if plan.status == "night" and morning_check and morning_check <= now < morning_check + AT_WINDOW:
        out.append(Reminder(
            f"morning:{morning_check.date().isoformat()}",
            f"☀️ {name} уже {gendered(gender, 'проснулся', 'проснулась')}? "
            "Скажите или нажмите «☀️ Проснулись», чтобы я рассчитал день.",
        ))

    if plan.status == "nap" and plan.naps:
        current = next((n for n in plan.naps if n.started and not n.finished), None)
        if current and norm.nap_max and now >= current.start + _hours(norm.nap_max):
            out.append(Reminder(
                f"long:{current.event_id}",
                f"⏰ Дневной сон идёт уже {format_duration(now - current.start)}. "
                "Можно аккуратно будить, чтобы вечером легко уснуть.",
            ))

    if plan.status != "awake" or plan.morning_wake is None:
        return out

    day = plan.morning_wake.id
    targets: list[tuple[str, datetime, str]] = []
    for i, nap in enumerate(plan.naps):
        if not nap.started and not nap.skipped:
            targets.append((f"{day}:nap{i}@{nap.start:%H%M}", nap.start, "дневной сон"))
            break  # напоминаем только о ближайшем сне
    else:
        if plan.bedtime:
            targets.append((f"{day}:bed@{plan.bedtime:%H%M}", plan.bedtime, "отбой"))

    for key, at, what in targets:
        pre = at - before
        if before > timedelta(0) and pre <= now < min(pre + PRE_WINDOW, at):
            minutes = round(before.total_seconds() / 60)
            out.append(Reminder(f"{key}:pre", f"🔔 Через {minutes} мин {what} — в {_fmt(at, tz)}."))
        if at <= now < at + AT_WINDOW:
            out.append(Reminder(f"{key}:now", f"😴 Пора укладывать: {what} ({_fmt(at, tz)})."))
    return out


def format_duration(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} ч {m} мин"
    return f"{h} ч" if h else f"{m} мин"
