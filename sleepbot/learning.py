"""Личный режим: по тому, как ребёнок засыпает, просыпается и спит ночью, подстраивает
окна бодрствования под него. Предложения делаются раз в неделю, применяются после согласия.

Логика:
- засыпает дольше 30 минут — не успел устать: окно перед этим сном коротковато;
- вырубается за 10 минут, но просыпается с плачем или ночью просыпается 2+ раза —
  похоже на переутомление: окно длинновато;
- засыпает за 20 минут и просыпается спокойным — время подходит.
Шаг — 15 минут, личная поправка не дальше ±1,5 часа от возрастной нормы.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from .schedule import NAP, NIGHT, WAKE, AgeNorm, Event, format_duration, gendered

STEP = 15  # минут за одно предложение
LIMIT = 90  # личная поправка не дальше этого от нормы (полтора часа)
MIN_SAMPLES = 4  # столько оценённых снов нужно для вывода
LOOKBACK = timedelta(days=14)
REVIEW_EVERY = timedelta(days=7)

MOODS = {"good": "😊", "ok": "😐", "bad": "😫"}


def personal_norm(norm: AgeNorm, adj_nap: int, adj_bed: int) -> AgeNorm:
    """Возрастная норма с личными поправками окон: перед дневными снами и перед ночью."""
    if not adj_nap and not adj_bed:
        return norm
    windows = [w + adj_nap / 60 for w in norm.wake_windows[:-1]] + [norm.wake_windows[-1] + adj_bed / 60]
    return replace(norm, wake_windows=tuple(windows))


@dataclass(frozen=True)
class Sample:
    kind: str  # NAP или NIGHT
    start: datetime
    awake: timedelta  # сколько бодрствовал перед этим сном
    latency: int | None  # минут до засыпания
    mood: str | None  # настроение после этого сна
    wakings: int | None  # ночных пробуждений


def samples(events: list[Event], feedback: dict[int, dict], unusual: set[date], tz) -> list[Sample]:
    """Сны с отзывами родителей; дни с особой причиной (болезнь, зубы) пропускаем."""
    events = sorted(events, key=lambda e: e.ts)
    out = []
    for prev, e in zip(events, events[1:]):
        fb = feedback.get(e.id)
        if e.kind not in (NAP, NIGHT) or prev.kind != WAKE or not fb:
            continue
        if e.ts.astimezone(tz).date() in unusual:
            continue
        awake = e.ts - prev.ts
        if timedelta(minutes=30) <= awake <= timedelta(hours=16):
            out.append(Sample(e.kind, e.ts, awake, fb.get("latency"), fb.get("mood"), fb.get("wakings")))
    return out


def verdict(s: Sample) -> str | None:
    """"early" — уложили рано (не устал), "late" — поздно (переутомился), "good" — в самый раз."""
    overtired_signs = s.mood == "bad" or (s.kind == NIGHT and (s.wakings or 0) >= 2)
    if s.latency is not None and s.latency >= 30:
        return "early"
    if s.latency is not None and s.latency <= 10 and overtired_signs:
        return "late"
    if s.latency is None and s.mood == "bad" and s.kind == NIGHT and (s.wakings or 0) >= 2:
        return "late"
    if overtired_signs:
        return None
    if (s.latency is not None and s.latency <= 20) or s.mood == "good":
        return "good"
    return None


@dataclass(frozen=True)
class Proposal:
    target: str  # "nap" или "bed"
    delta: int  # минуты: плюс — бодрствовать дольше (позже), минус — меньше (раньше)
    text: str


@dataclass(frozen=True)
class Review:
    proposals: list[Proposal]
    summary: list[str]  # что заметили, даже если менять нечего


def review(items: list[Sample], now: datetime, norm: AgeNorm, adj_nap: int, adj_bed: int,
           name: str, gender: str | None) -> Review:
    proposals, summary = [], []
    he = gendered(gender, "он", "она")
    targets = [("bed", NIGHT, adj_bed, norm.wake_windows[-1], "ночным сном", "отбой")]
    if norm.naps:
        targets.insert(0, ("nap", NAP, adj_nap, norm.wake_windows[0], "дневным сном", "дневной сон"))
    for target, kind, adj, base_hours, before, what in targets:
        recent = [s for s in items if s.kind == kind and now - s.start <= LOOKBACK]
        known = [v for v in map(verdict, recent) if v]
        if len(known) < MIN_SAMPLES:
            continue
        early, late, good = known.count("early"), known.count("late"), known.count("good")
        total = len(known)
        current = timedelta(hours=base_hours) + timedelta(minutes=adj)
        delta, why = 0, ""
        if early * 2 >= total and early > late:
            delta = STEP
            why = (f"в {early} из {total} случаев {name} засыпал(а) дольше 30 минут — похоже, "
                   f"{he} ещё не успевает устать")
        elif late * 2 >= total and late > early:
            delta = -STEP
            why = (f"в {late} из {total} случаев {name} засыпал(а) мгновенно, но потом плакал(а) "
                   "или часто просыпался(ась) — похоже на переутомление")
        new_adj = max(-LIMIT, min(LIMIT, adj + delta))
        if delta and new_adj != adj:
            new = timedelta(hours=base_hours) + timedelta(minutes=new_adj)
            way = "позже" if delta > 0 else "раньше"
            proposals.append(Proposal(target, new_adj - adj, (
                f"Перед {before}: {why}. Предлагаю {what} на {abs(new_adj - adj)} мин {way}: "
                f"бодрствование {format_duration(new)} вместо {format_duration(current)}.")))
        elif delta:
            summary.append(f"Перед {before}: {why}, но поправка уже предельная (±{LIMIT} мин) — "
                           "дальше двигать не буду. Если так продолжится, стоит обсудить с педиатром.")
        elif good * 2 >= total:
            summary.append(f"Перед {before}: в {good} из {total} случаев засыпал(а) спокойно и "
                           f"просыпался(ась) в хорошем настроении — бодрствование "
                           f"{format_duration(current)} подходит.")
        else:
            summary.append(f"Перед {before}: картина пока неоднозначная ({good} хорошо, "
                           f"{early} не устал(а), {late} переутомился(ась)) — оставляю как есть.")
    proposals = [replace(p, text=_fix(p.text, gender)) for p in proposals]
    return Review(proposals, [_fix(line, gender) for line in summary])


def _fix(text: str, gender: str | None) -> str:
    """«засыпал(а)» → по полу ребёнка."""
    for both, masc, fem in (("засыпал(а)", "засыпал", "засыпала"), ("плакал(а)", "плакал", "плакала"),
                            ("просыпался(ась)", "просыпался", "просыпалась"),
                            ("устал(а)", "устал", "устала"),
                            ("переутомился(ась)", "переутомился", "переутомилась")):
        text = text.replace(both, gendered(gender, masc, fem) if gender else both)
    return text
