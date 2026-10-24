"""Telegram-бот: голос, кнопки и текст → события сна, план дня, ответы и напоминания."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

from . import onboarding
from .assistant import Assistant, parse_hhmm, resolve_time
from .parsing import (
    is_question, normalize, parse_birth_date, parse_clock, parse_event, question_topic,
)
from .schedule import (
    NAP, NIGHT, WAKE, AgeNorm, DayPlan, Event, age_in_months, build_plan, classify_sleep,
    due_reminders, format_age, format_duration, gendered, norm_for,
)
from .storage import Chat, Storage
from .stt import Transcriber

log = logging.getLogger(__name__)

BTN_WAKE = "☀️ Проснулись"
BTN_SLEEP = "😴 Уснули"
BTN_PLAN = "📅 План"
BTN_TODAY = "📊 Сегодня"
KEYBOARD = ReplyKeyboardMarkup([[BTN_WAKE, BTN_SLEEP], [BTN_PLAN, BTN_TODAY]], resize_keyboard=True)

# Глагол в прошедшем времени — это отметка, даже если дальше идёт вопрос
# («проснулся, когда теперь спать?»). «Когда ему засыпать?» — только вопрос.
_PAST_EVENT = re.compile(r"просну\w*л|проснул|уснул|заснул|встал|л[её]г\b|легл|уложил|разбудил", re.I)

HELP = (
    "Говорите голосовым или пишите:\n"
    "• «проснулся», «проснулась в 6:50», «уснул 10 минут назад», «легли на ночь в 20:15»\n"
    "• «когда спать?», «во сколько отбой?», «сколько он спал?»\n"
    "Можно и кнопками внизу. Дневной это сон или ночной, я определяю сам; если ошибся — "
    "поправьте кнопкой под сообщением.\n\n"
    "Команды:\n"
    "/plan — план на сегодня\n"
    "/today — сон за сутки\n"
    "/week — сон за неделю\n"
    "/undo — удалить последнюю отметку\n"
    "/setup — заполнить анкету заново\n"
    "/tz Europe/Moscow — часовой пояс\n"
    "/remind 30 — за сколько минут напоминать (0 — только в момент укладывания)\n"
    "/morning 08:30 — спросить про подъём, если он не отмечен (off — не спрашивать)"
)


def store(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.application.bot_data["storage"]


def tz_of(chat: Chat):
    if m := re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", chat.tz):
        offset = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return timezone(offset if m.group(1) == "+" else -offset, chat.tz)
    return ZoneInfo(chat.tz)


def norm_of(chat: Chat, now: datetime) -> AgeNorm:
    return norm_for(age_in_months(chat.birth_date, now.date()), chat.naps_pref)


def hm(dt: datetime, tz) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def g(chat: Chat, masc: str, fem: str) -> str:
    return gendered(chat.gender, masc, fem)


def day_plan(chat: Chat, s: Storage, now: datetime) -> DayPlan:
    return build_plan(s.recent(chat.chat_id, now), now, norm_of(chat, now), s.overrides(chat.chat_id))


def expected_plan(chat: Chat, now: datetime) -> DayPlan | None:
    """План на случай подъёма в обычное время — пока утренний подъём не отмечен."""
    if not chat.wake_usual:
        return None
    h, m = map(int, chat.wake_usual.split(":"))
    wake = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if wake < now - timedelta(hours=12):
        wake += timedelta(days=1)
    return build_plan([Event(0, WAKE, wake)], wake, norm_of(chat, now))


def schedule_lines(plan: DayPlan, norm: AgeNorm, tz, now: datetime) -> list[str]:
    lines = [f"☀️ Подъём — {hm(plan.morning_wake.ts, tz)}"]
    for i, nap in enumerate(plan.naps, 1):
        title = "Дневной сон" if len(plan.naps) == 1 else f"Сон {i}"
        if nap.skipped:
            lines.append(f"😴 {title} — пропущен (был в плане на {hm(nap.start, tz)})")
        elif nap.finished:
            lines.append(f"😴 {title} — {hm(nap.start, tz)}–{hm(nap.end, tz)} "
                         f"({format_duration(nap.end - nap.start)}) ✓")
        elif nap.started:
            lines.append(f"😴 {title} — спит с {hm(nap.start, tz)}, разбудить не позже "
                         f"{hm(nap.start + timedelta(hours=norm.nap_max), tz)}")
        else:
            mark = " (назначено вами)" if f"nap{i - 1}" in plan.manual else ""
            lines.append(f"😴 {title} — {hm(nap.start, tz)}–{hm(nap.end, tz)}{mark}")
    mark = " (назначено вами)" if "bed" in plan.manual else ""
    lines.append(f"🌙 Отбой — {hm(plan.bedtime, tz)}{mark}")
    return lines


def plan_text(chat: Chat, plan: DayPlan, now: datetime) -> str:
    tz = tz_of(chat)
    months = age_in_months(chat.birth_date, now.date())
    norm = norm_of(chat, now)
    lines = [f"📅 План на сегодня — {chat.child_name}, {format_age(months)}"]

    if plan.morning_wake is None:
        if plan.status == "night":
            lines.append(f"🌙 Ночной сон с {hm(plan.status_since, tz)}, "
                         f"идёт {format_duration(now - plan.status_since)}. "
                         f"Норма в этом возрасте — {norm.night_hours} ч.")
        else:
            lines.append("Утренний подъём не отмечен.")
        if expected := expected_plan(chat, now):
            lines.append(f"\nЕсли проснётся в {chat.wake_usual}:")
            lines += schedule_lines(expected, norm, tz, now)[1:]
        lines.append("\nУтром скажите «проснулся» — и я рассчитаю день точно.")
        return "\n".join(lines)

    lines += schedule_lines(plan, norm, tz, now)
    lines += night_lines(chat, plan, norm)
    if any(n.skipped for n in plan.naps):
        lines.append("\nДневной сон пропущен, поэтому отбой раньше обычного.")
    if plan.status == "awake":
        lines.append(f"\nСейчас бодрствует {format_duration(now - plan.status_since)}.")
    return "\n".join(lines)


def night_lines(chat: Chat, plan: DayPlan, norm: AgeNorm) -> list[str]:
    """Сколько спал ночью и как это сдвинуло день."""
    if plan.night is None:
        return [f"\nНе знаю, во сколько {g(chat, 'он уснул', 'она уснула')} вчера. "
                "Скажите, например, «в 21:15» — учту, сколько длилась ночь."]
    lines = [f"\nНочью {g(chat, 'спал', 'спала')} {format_duration(plan.night)} "
             f"(норма {norm.night_hours} ч)."]
    if plan.nap_shift or plan.bed_shift:
        moved = []
        if plan.nap_shift:
            moved.append(f"дневной сон на {format_duration(plan.nap_shift)} раньше")
        if plan.bed_shift:
            moved.append(f"отбой на {format_duration(plan.bed_shift)} раньше")
        lines.append(f"Ночь короче нормы, {g(chat, 'он', 'она')} устанет раньше: "
                     + " и ".join(moved) + ".")
    return lines


def night_start_answer(chat: Chat, s: Storage, text: str, now: datetime) -> datetime | None:
    """«В 21:15» после утреннего подъёма — во сколько ребёнок уснул вчера вечером."""
    plan = day_plan(chat, s, now)
    clock = parse_clock(normalize(text))
    if plan.morning_wake is None or plan.night is not None or clock is None:
        return None
    wake = plan.morning_wake.ts.astimezone(tz_of(chat))
    hour, minute = clock
    for h in (hour, hour + 12) if hour < 12 else (hour,):
        at = wake.replace(hour=h, minute=minute, second=0, microsecond=0)
        if at >= wake:
            at -= timedelta(days=1)
        if timedelta(hours=3) <= wake - at <= timedelta(hours=16):
            return at
    return None


def answer_question(chat: Chat, s: Storage, text: str, now: datetime) -> str:
    """Ответ на «когда спать?», «во сколько отбой?», «сколько спал?»."""
    topic = question_topic(text)
    if topic == "history":
        return today_text(chat, s, now)
    tz = tz_of(chat)
    plan = day_plan(chat, s, now)
    name = chat.child_name

    if plan.status == "nap":
        nap = next(n for n in plan.naps if n.started and not n.finished)
        head = (f"{name} спит с {hm(nap.start, tz)}. Будить не позже "
                f"{hm(nap.start + timedelta(hours=norm_of(chat, now).nap_max), tz)}, "
                f"отбой примерно в {hm(plan.bedtime, tz)}.")
    elif plan.morning_wake is None:
        head = (f"{name} спит ночью." if plan.status == "night"
                else "Подъём сегодня не отмечен — не от чего считать.")
    else:
        upcoming = next((n for n in plan.naps if not n.started and not n.skipped), None)
        if upcoming and topic != "night":
            head = _when("Дневной сон", upcoming.start, now, tz)
        else:
            head = _when("Отбой", plan.bedtime, now, tz)
    return head + "\n\n" + plan_text(chat, plan, now)


def _when(what: str, at: datetime, now: datetime, tz) -> str:
    if at > now:
        return f"{what} в {hm(at, tz)} — через {format_duration(at - now)}."
    return f"Пора укладывать! {what} был по плану в {hm(at, tz)} ({format_duration(now - at)} назад)."


def sessions(events: list[Event]) -> list[tuple[Event, datetime | None]]:
    """Пары «уснул → проснулся»."""
    out = []
    for i, e in enumerate(events):
        if e.kind in (NAP, NIGHT):
            nxt = events[i + 1] if i + 1 < len(events) else None
            out.append((e, nxt.ts if nxt and nxt.kind == WAKE else None))
    return out


def today_text(chat: Chat, s: Storage, now: datetime) -> str:
    tz = tz_of(chat)
    since = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=6)
    events = s.events(chat.chat_id, since=since)
    if not events:
        return "За сутки отметок нет."
    lines = ["📊 Сон с вечера вчерашнего дня:"]
    total_nap = timedelta()
    for e, end in sessions(events):
        icon, title = ("🌙", "Ночь") if e.kind == NIGHT else ("😴", "Днём")
        if end:
            lines.append(f"{icon} {title} {hm(e.ts, tz)}–{hm(end, tz)} ({format_duration(end - e.ts)})")
            if e.kind == NAP:
                total_nap += end - e.ts
        else:
            lines.append(f"{icon} {title} с {hm(e.ts, tz)} — спит")
    if total_nap:
        lines.append(f"\nДневной сон всего: {format_duration(total_nap)}")
    return "\n".join(lines)


def week_text(chat: Chat, s: Storage, now: datetime) -> str:
    tz = tz_of(chat)
    start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    events = s.events(chat.chat_id, since=start - timedelta(hours=14))
    days: dict = {}
    for e, end in sessions(events):
        if not end:
            continue
        # Ночь относим к дню, когда ребёнок проснулся.
        day = end.astimezone(tz).date() if e.kind == NIGHT else e.ts.astimezone(tz).date()
        if day < start.date():
            continue
        night, nap = days.get(day, (timedelta(), timedelta()))
        days[day] = (night + (end - e.ts), nap) if e.kind == NIGHT else (night, nap + (end - e.ts))
    if not days:
        return "За неделю нет завершённых снов."
    lines = ["📈 Сон за неделю (ночь / днём):"]
    for day in sorted(days):
        night, nap = days[day]
        lines.append(f"{day.strftime('%d.%m')}: 🌙 {format_duration(night) if night else '—'} / "
                     f"😴 {format_duration(nap) if nap else '—'}")
    return "\n".join(lines)


def event_keyboard(event: Event) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"−{m} мин", callback_data=f"adj:{event.id}:{m}") for m in (5, 10, 15, 30)]]
    if event.kind == NAP:
        rows.append([InlineKeyboardButton("Это ночной сон", callback_data=f"kind:{event.id}:{NIGHT}")])
    elif event.kind == NIGHT:
        rows.append([InlineKeyboardButton("Это дневной сон", callback_data=f"kind:{event.id}:{NAP}")])
    rows.append([InlineKeyboardButton("Отменить", callback_data=f"del:{event.id}")])
    return InlineKeyboardMarkup(rows)


def event_text(chat: Chat, s: Storage, event: Event, now: datetime) -> str:
    tz = tz_of(chat)
    events = s.recent(chat.chat_id, now)
    name = chat.child_name
    if event.kind == WAKE:
        prev = [e for e in events if e.ts < event.ts]
        head = f"☀️ {name} {g(chat, 'проснулся', 'проснулась')} в {hm(event.ts, tz)}"
        if prev and prev[-1].kind in (NAP, NIGHT):
            head += f" — {g(chat, 'спал', 'спала')} {format_duration(event.ts - prev[-1].ts)}"
    elif event.kind == NAP:
        head = f"😴 {name} {g(chat, 'уснул', 'уснула')} днём в {hm(event.ts, tz)}"
    else:
        head = f"🌙 {name} {g(chat, 'уснул', 'уснула')} на ночь в {hm(event.ts, tz)}. Спокойной ночи!"
    return head + "\n\n" + plan_text(chat, day_plan(chat, s, now), now)


# --- анкета ---------------------------------------------------------------------------


async def ask(update: Update, chat: Chat, step: str, prefix: str = "") -> None:
    q = onboarding.question(step, chat.child_name, chat.gender)
    markup = (ReplyKeyboardMarkup([q.options[i:i + 2] for i in range(0, len(q.options), 2)],
                                  resize_keyboard=True, one_time_keyboard=True)
              if q.options else ReplyKeyboardRemove())
    await update.effective_message.reply_text(prefix + q.text, reply_markup=markup)


async def begin_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context)
    s.update_chat(update.effective_chat.id, setup_step="name")
    await ask(update, s.chat(update.effective_chat.id), "name",
              "Привет! Я помогу следить за сном малыша: вы говорите мне голосом, когда он "
              "проснулся или уснул, а я рассчитываю дневной сон и отбой по возрасту "
              "и заранее напоминаю.\n\nСначала пара вопросов — можно отвечать голосовыми.\n\n")


async def handle_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, chat: Chat,
                       text: str, heard: str) -> None:
    s = store(context)
    now = datetime.now(tz_of(chat))
    answer = onboarding.apply(
        chat.setup_step, text, {"gender": chat.gender, "birth_date": chat.birth_date},
        now.date(), datetime.now(timezone.utc),
    )
    if answer.error:
        await ask(update, chat, chat.setup_step, heard + answer.error + "\n\n")
        return
    updates = dict(answer.updates)
    if "wake_usual" in updates and not updates["wake_usual"]:
        updates["morning_check"] = "09:00"  # подъём «по-разному» — спрашиваем попозже
    if wake := updates.get("wake_usual"):
        h, m = map(int, wake.split(":"))
        updates["morning_check"] = f"{h + 1:02d}:{m:02d}"
    s.update_chat(chat.chat_id, setup_step=answer.next_step, **updates)
    chat = s.chat(chat.chat_id)
    if answer.next_step:
        await ask(update, chat, answer.next_step, heard)
        return
    await update.effective_message.reply_text(heard + setup_summary(chat), reply_markup=KEYBOARD)


def setup_summary(chat: Chat) -> str:
    tz = tz_of(chat)
    now = datetime.now(tz)
    months = age_in_months(chat.birth_date, now.date())
    norm = norm_of(chat, now)
    sex = {"m": "мальчик", "f": "девочка"}.get(chat.gender, "")
    windows = ", ".join(format_duration(timedelta(hours=w)) for w in norm.wake_windows)
    who = ", ".join(x for x in (chat.child_name, sex, format_age(months)) if x)
    lines = [
        f"Готово! {who}.",
        f"Дневных снов: {len(norm.naps)}, окна бодрствования: {windows}, "
        f"ночной сон: {norm.night_hours} ч. Часовой пояс: {chat.tz}.",
    ]
    if expected := expected_plan(chat, now):
        lines.append(f"\nПри подъёме в {chat.wake_usual} примерный день:")
        lines += schedule_lines(expected, norm, tz, now)[1:]
    lines += [
        f"\nНапомню за {chat.remind_before} мин до сна и в момент укладывания.",
        "\nКак пользоваться — просто запишите голосовое:",
        f"• «{g(chat, 'проснулся', 'проснулась')}» / «{g(chat, 'уснул', 'уснула')}» "
        "(можно со временем: «в 7:20», «15 минут назад»)",
        "• «когда спать?», «во сколько отбой?»",
        "/help — все команды, /setup — изменить анкету.",
    ]
    return "\n".join(lines)


# --- команды --------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = store(context).chat(update.effective_chat.id)
    if chat.configured:
        await update.effective_message.reply_text(HELP, reply_markup=KEYBOARD)
    elif chat.setup_step:
        await ask(update, chat, chat.setup_step)
    else:
        await begin_setup(update, context)


async def setup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await begin_setup(update, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, reply_markup=KEYBOARD)


async def child_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args)
    birth = parse_birth_date(text)
    name = re.sub(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}", "", text).strip(" ,")
    if not birth or not name:
        await update.effective_message.reply_text("Формат: /child Миша 12.03.2024 (или /setup — анкета)")
        return
    s = store(context)
    s.update_chat(update.effective_chat.id, child_name=name, birth_date=birth)
    await update.effective_message.reply_text(setup_summary(s.chat(update.effective_chat.id)),
                                              reply_markup=KEYBOARD)


async def tz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = context.args[0] if context.args else ""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        await update.effective_message.reply_text(
            "Формат: /tz Europe/Moscow (или Asia/Yekaterinburg, Europe/Berlin…)"
        )
        return
    store(context).update_chat(update.effective_chat.id, tz=name)
    await update.effective_message.reply_text(f"Часовой пояс: {name}")


async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Формат: /remind 30 — минут до сна")
        return
    minutes = min(int(context.args[0]), 180)
    store(context).update_chat(update.effective_chat.id, remind_before=minutes)
    await update.effective_message.reply_text(
        f"Буду напоминать за {minutes} мин и в момент укладывания." if minutes
        else "Заранее не напоминаю, только в момент укладывания."
    )


async def morning_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = context.args[0] if context.args else ""
    if arg.lower() == "off":
        store(context).update_chat(update.effective_chat.id, morning_check=None)
        await update.effective_message.reply_text("Утром про подъём не спрашиваю.")
        return
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", arg):
        await update.effective_message.reply_text("Формат: /morning 08:30 или /morning off")
        return
    store(context).update_chat(update.effective_chat.id, morning_check=arg.zfill(5))
    await update.effective_message.reply_text(f"Если к {arg.zfill(5)} подъём не отмечен — спрошу.")


async def configured_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Chat | None:
    chat = store(context).chat(update.effective_chat.id)
    if chat.configured:
        return chat
    if chat.setup_step:
        await ask(update, chat, chat.setup_step, "Сначала закончим анкету.\n\n")
    else:
        await begin_setup(update, context)
    return None


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if chat := await configured_chat(update, context):
        s = store(context)
        now = datetime.now(tz_of(chat))
        plan = day_plan(chat, s, now)
        await update.effective_message.reply_text(plan_text(chat, plan, now), reply_markup=KEYBOARD)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if chat := await configured_chat(update, context):
        await update.effective_message.reply_text(
            today_text(chat, store(context), datetime.now(tz_of(chat))))


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if chat := await configured_chat(update, context):
        await update.effective_message.reply_text(
            week_text(chat, store(context), datetime.now(tz_of(chat))))


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    last = s.last_event(chat.chat_id)
    if not last:
        await update.effective_message.reply_text("Отметок нет.")
        return
    s.delete_event(chat.chat_id, last.id)
    what = {WAKE: "подъём", NAP: "дневной сон", NIGHT: "ночной сон"}[last.kind]
    await update.effective_message.reply_text(f"Удалил: {what} в {hm(last.ts, tz_of(chat))}.")


# --- свободный диалог через Claude -----------------------------------------------------


class ChatTools:
    """Действия, которые ассистент выполняет в базе бота для одного чата."""

    KINDS = {WAKE: "проснулся(ась)", NAP: "уснул(а) днём", NIGHT: "уснул(а) на ночь"}

    def __init__(self, chat: Chat, s: Storage, now: datetime) -> None:
        self.chat, self.s, self.now = chat, s, now
        self.changed: list[int] = []  # события, затронутые в этом сообщении, — для кнопок
        self.plan_changed = False  # менялись ли время сна или настройки

    @property
    def tz(self):
        return tz_of(self.chat)

    def _describe(self, kind: str, at: datetime) -> str:
        return f"{self.KINDS[kind]} в {hm(at, self.tz)} ({at.astimezone(self.tz):%d.%m})"

    def _when(self, time: str, day: str) -> datetime:
        at = resolve_time(time, day, self.now)
        if at > self.now + timedelta(minutes=5):
            raise ValueError("это время ещё не наступило")
        return at

    def _kind(self, kind: str, at: datetime) -> str:
        if kind == "sleep":
            kind = classify_sleep(self.s.recent(self.chat.chat_id, self.now), at,
                                  norm_of(self.chat, self.now), at.astimezone(self.tz).hour)
        if kind not in self.KINDS:
            raise ValueError(f"неизвестный тип события {kind}")
        return kind

    def record_event(self, kind: str, time: str, day: str) -> str:
        at = self._when(time, day)
        kind = self._kind(kind, at)
        event_id = self.s.add_event(self.chat.chat_id, kind, at)
        self.changed.append(event_id)
        return f"Записано #{event_id}: {self._describe(kind, at)}."

    def update_event(self, event_id: int, kind: str, time: str, day: str) -> str:
        event = self.s.event(self.chat.chat_id, event_id)
        if not event:
            raise ValueError(f"отметки #{event_id} нет")
        at = event.ts if time == "keep" else self._when(time, day)
        kind = event.kind if kind == "keep" else self._kind(kind, at)
        self.s.update_event(self.chat.chat_id, event_id, kind=kind, ts=at)
        self.changed.append(event_id)
        return f"Исправлено #{event_id}: было {self._describe(event.kind, event.ts)}, " \
               f"стало {self._describe(kind, at)}."

    def delete_event(self, event_id: int) -> str:
        event = self.s.event(self.chat.chat_id, event_id)
        if not event:
            raise ValueError(f"отметки #{event_id} нет")
        self.s.delete_event(self.chat.chat_id, event_id)
        self.plan_changed = True
        return f"Удалено #{event_id}: {self._describe(event.kind, event.ts)}."

    def set_plan_time(self, target: str, time: str) -> str:
        plan = day_plan(self.chat, self.s, self.now)
        if plan.morning_wake is None:
            raise ValueError("сегодняшний подъём не отмечен — сначала нужен подъём")
        if target == "nap":
            index = next((i for i, n in enumerate(plan.naps) if not n.started), None)
            if index is None:
                raise ValueError("на сегодня запланированных дневных снов не осталось")
            key, what = f"nap{index}", "дневной сон"
        elif target == "bedtime":
            key, what = "bed", "отбой"
        else:
            raise ValueError(f"неизвестная цель {target}")
        if time == "auto":
            self.s.set_override(self.chat.chat_id, plan.morning_wake.id, key, None)
            self.plan_changed = True
            return f"{what.capitalize()} снова считается автоматически."
        at = resolve_time(time, "today", self.now)
        if at < self.now - timedelta(minutes=5):
            at += timedelta(days=1)  # «отбой в 0:30» — это уже после полуночи
        self.s.set_override(self.chat.chat_id, plan.morning_wake.id, key, at)
        self.plan_changed = True
        return f"На сегодня {what} назначен на {hm(at, self.tz)}; напоминания придут к этому времени."

    def update_settings(self, setting: str, value: str) -> str:
        chat_id, v = self.chat.chat_id, value.strip()
        if setting == "child_name":
            self.s.update_chat(chat_id, child_name=v[:40])
        elif setting == "gender":
            if v not in ("m", "f"):
                raise ValueError("пол: m или f")
            self.s.update_chat(chat_id, gender=v)
        elif setting == "birth_date":
            birth = parse_birth_date(v)
            if not birth or birth > self.now.date():
                raise ValueError("дата рождения: ДД.ММ.ГГГГ")
            self.s.update_chat(chat_id, birth_date=birth)
        elif setting == "naps_per_day":
            if v == "auto":
                self.s.update_chat(chat_id, naps_pref=None)
            elif v in ("0", "1", "2", "3"):
                self.s.update_chat(chat_id, naps_pref=int(v))
            else:
                raise ValueError("число дневных снов: 0–3 или auto")
        elif setting == "usual_wake":
            if v == "none":
                self.s.update_chat(chat_id, wake_usual=None)
            elif hm_ := parse_hhmm(v):
                self.s.update_chat(chat_id, wake_usual=f"{hm_[0]:02d}:{hm_[1]:02d}")
            else:
                raise ValueError("обычный подъём: ЧЧ:ММ или none")
        elif setting == "remind_before_minutes":
            if not v.isdigit() or int(v) > 180:
                raise ValueError("минуты: 0–180")
            self.s.update_chat(chat_id, remind_before=int(v))
        elif setting == "morning_check":
            if v == "off":
                self.s.update_chat(chat_id, morning_check=None)
            elif hm_ := parse_hhmm(v):
                self.s.update_chat(chat_id, morning_check=f"{hm_[0]:02d}:{hm_[1]:02d}")
            else:
                raise ValueError("утренний вопрос: ЧЧ:ММ или off")
        elif setting == "timezone":
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("часовой пояс в формате Europe/Moscow") from None
            self.s.update_chat(chat_id, tz=v)
        else:
            raise ValueError(f"неизвестная настройка {setting}")
        self.chat = self.s.chat(chat_id)
        self.plan_changed = True
        return f"Настройка {setting} = {v} сохранена."

    def state(self) -> str:
        chat, tz, now = self.chat, self.tz, self.now
        months = age_in_months(chat.birth_date, now.date())
        sex = {"m": "мальчик", "f": "девочка"}.get(chat.gender, "пол не указан")
        norm = norm_of(chat, now)
        lines = [
            f"Сейчас {now:%d.%m %H:%M} ({chat.tz}).",
            f"Ребёнок: {chat.child_name}, {sex}, {format_age(months)}, родился {chat.birth_date:%d.%m.%Y}.",
            f"Норма по возрасту: дневных снов {len(norm.naps)}, ночной сон {norm.night_hours} ч.",
            f"Настройки: напоминать за {chat.remind_before} мин; обычный подъём "
            f"{chat.wake_usual or 'по-разному'}; утренний вопрос {chat.morning_check or 'выключен'}; "
            f"дневных снов по словам родителей: "
            f"{chat.naps_pref if chat.naps_pref is not None else 'по возрасту'}.",
        ]
        events = self.s.events(chat.chat_id, since=now - timedelta(hours=36))
        lines.append("Отметки за полтора дня: " + ("; ".join(
            f"#{e.id} {e.ts.astimezone(tz):%d.%m %H:%M} {self.KINDS[e.kind]}" for e in events)
            or "нет") + ".")
        lines.append(plan_text(chat, day_plan(chat, self.s, now), now))
        return "\n".join(lines)


async def handle_with_assistant(update: Update, context: ContextTypes.DEFAULT_TYPE, chat: Chat,
                                text: str, heard: str) -> bool:
    """True, если сообщение обработал Claude; False — пусть разбирают правила."""
    assistant: Assistant | None = context.application.bot_data.get("assistant")
    if assistant is None:
        return False
    s = store(context)
    now = datetime.now(tz_of(chat))
    tools = ChatTools(chat, s, now)
    try:
        reply_text = await assistant.handle(chat.chat_id, text, tools.state(), tools)
    except Exception:
        if not (tools.changed or tools.plan_changed):
            log.exception("Claude недоступен, разбираю сообщение правилами")
            return False
        # Изменения уже внесены — повторный разбор правилами записал бы их второй раз.
        log.exception("Claude оборвался после изменений")
        reply_text = "Изменения внесены, но ответ не получился. Проверьте план ниже."
    reply = heard + reply_text
    events = [e for e in (s.event(chat.chat_id, i) for i in reversed(tools.changed)) if e]
    event = events[0] if events else None
    if tools.changed or tools.plan_changed:
        chat = s.chat(chat.chat_id)
        reply += "\n\n" + plan_text(chat, day_plan(chat, s, now), now)
    await update.effective_message.reply_text(
        reply, reply_markup=event_keyboard(event) if event else KEYBOARD)
    return True


# --- сообщения ------------------------------------------------------------------------


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                      voice: bool = False) -> None:
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    heard = f"🎤 «{text}»\n\n" if voice else ""
    private = update.effective_chat.type == "private"

    if text == BTN_PLAN:
        await plan_cmd(update, context)
        return
    if text == BTN_TODAY:
        await today_cmd(update, context)
        return
    if chat.setup_step:
        await handle_setup(update, context, chat, text, heard)
        return
    if not chat.configured:
        await begin_setup(update, context)
        return

    # Кнопки разбираем сами — мгновенно и бесплатно; всё остальное — через Claude, если он есть.
    if text not in (BTN_WAKE, BTN_SLEEP) and await handle_with_assistant(
            update, context, chat, text, heard):
        return

    tz = tz_of(chat)
    now = datetime.now(tz)
    parsed = parse_event(text, now)
    if is_question(text) and not (parsed and _PAST_EVENT.search(text)):
        await update.effective_message.reply_text(heard + answer_question(chat, s, text, now),
                                                  reply_markup=KEYBOARD)
        return
    if not parsed and (night := night_start_answer(chat, s, text, now)):
        s.add_event(chat.chat_id, NIGHT, night)
        plan = day_plan(chat, s, now)
        await update.effective_message.reply_text(
            heard + f"🌙 Записал: {g(chat, 'уснул', 'уснула')} вчера в {hm(night, tz)}.\n\n"
            + plan_text(chat, plan, now), reply_markup=KEYBOARD)
        return
    if not parsed:
        if private or voice:
            await update.effective_message.reply_text(
                heard + "Не понял. Скажите, например, «проснулся», «уснула 5 минут назад» "
                "или «когда спать?». /help"
            )
        return

    if parsed.action == WAKE:
        kind = WAKE
    else:
        events = s.recent(chat.chat_id, now)
        kind = parsed.kind or classify_sleep(events, parsed.at, norm_of(chat, now),
                                             parsed.at.astimezone(tz).hour)
    event_id = s.add_event(chat.chat_id, kind, parsed.at)
    event = s.event(chat.chat_id, event_id)
    await update.effective_message.reply_text(
        heard + event_text(chat, s, event, now), reply_markup=event_keyboard(event)
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_text(update, context, update.effective_message.text or "")


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    media = message.voice or message.audio
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "voice.ogg")
        try:
            file = await media.get_file()
            await file.download_to_drive(path)
            text = await context.application.bot_data["stt"].transcribe(path)
        except Exception:
            log.exception("Не удалось распознать голосовое")
            await message.reply_text("Не получилось распознать голосовое 😕 Напишите текстом.")
            return
    if not text:
        await message.reply_text("Не расслышал. Повторите, пожалуйста.")
        return
    await handle_text(update, context, text, voice=True)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    action, event_id, arg = query.data.split(":")
    event = s.event(chat.chat_id, int(event_id))
    if not event:
        await query.answer("Эта отметка уже удалена")
        return
    tz = tz_of(chat)
    now = datetime.now(tz)
    if action == "del":
        s.delete_event(chat.chat_id, event.id)
        await query.answer("Удалено")
        await query.edit_message_text(f"Отметка в {hm(event.ts, tz)} удалена.")
        return
    if action == "adj":
        s.update_event(chat.chat_id, event.id, ts=event.ts - timedelta(minutes=int(arg)))
    elif action == "kind" and arg in (NAP, NIGHT):
        s.update_event(chat.chat_id, event.id, kind=arg)
    event = s.event(chat.chat_id, event.id)
    await query.answer()
    await query.edit_message_text(event_text(chat, s, event, now), reply_markup=event_keyboard(event))


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context)
    for chat in s.chats():
        if not chat.configured:
            continue
        try:
            tz = tz_of(chat)
            now = datetime.now(tz)
            norm = norm_of(chat, now)
            plan = build_plan(s.recent(chat.chat_id, now), now, norm, s.overrides(chat.chat_id))
            check = None
            if chat.morning_check:
                h, m = map(int, chat.morning_check.split(":"))
                check = now.replace(hour=h, minute=m, second=0, microsecond=0)
            for r in due_reminders(plan, now, tz, norm, timedelta(minutes=chat.remind_before),
                                   check, chat.child_name, chat.gender):
                if s.mark_sent(chat.chat_id, r.key):
                    await context.bot.send_message(chat.chat_id, r.text, reply_markup=KEYBOARD)
        except Exception:
            log.exception("Напоминание для чата %s не отправлено", chat.chat_id)


async def preload_model(app: Application) -> None:
    """Загружаем Whisper сразу при старте, чтобы первое голосовое не ждало."""
    app.create_task(asyncio.to_thread(app.bot_data["stt"].load))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ["BOT_TOKEN"]
    builder = Application.builder().token(token).concurrent_updates(True).post_init(preload_model)
    # Если api.telegram.org с сервера недоступен — через прокси: http://… или socks5://user:pass@host:port
    if proxy := os.environ.get("TELEGRAM_PROXY"):
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
    app = builder.build()
    app.bot_data["storage"] = Storage(os.environ.get("DB_PATH", "sleep.db"))
    app.bot_data["stt"] = Transcriber()
    app.bot_data["assistant"] = Assistant.from_env()
    log.info("Свободный диалог через Claude: %s",
             "включён" if app.bot_data["assistant"] else "выключен (нет ANTHROPIC_API_KEY)")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setup", setup_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("child", child_cmd))
    app.add_handler(CommandHandler("tz", tz_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("morning", morning_cmd))
    app.add_handler(CommandHandler("plan", plan_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(tick, interval=60, first=5)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
