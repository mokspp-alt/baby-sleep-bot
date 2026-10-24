"""Telegram-бот: кнопки и текст → события сна, план дня, напоминания."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

from .parsing import parse_birth_date, parse_event
from .schedule import (
    NAP, NIGHT, WAKE, AgeNorm, DayPlan, Event, age_in_months, build_plan, classify_sleep,
    due_reminders, format_age, format_duration, norm_for_age,
)
from .storage import Chat, Storage

log = logging.getLogger(__name__)

BTN_WAKE = "☀️ Проснулись"
BTN_SLEEP = "😴 Уснули"
BTN_PLAN = "📅 План"
BTN_TODAY = "📊 Сегодня"
KEYBOARD = ReplyKeyboardMarkup([[BTN_WAKE, BTN_SLEEP], [BTN_PLAN, BTN_TODAY]], resize_keyboard=True)

HELP = (
    "Отмечайте подъёмы и засыпания кнопками или текстом — можно надиктовать:\n"
    "• «проснулся в 6:50», «уснула 10 минут назад», «легли на ночь в 20:15»\n"
    "Дневной это сон или ночной, я определяю сам; если ошибся — поправьте кнопкой.\n\n"
    "Команды:\n"
    "/plan — план на сегодня\n"
    "/today — сон за сутки\n"
    "/week — сон за неделю\n"
    "/undo — удалить последнюю отметку\n"
    "/child Имя ДД.ММ.ГГГГ — имя и дата рождения\n"
    "/tz Europe/Moscow — часовой пояс\n"
    "/remind 30 — за сколько минут напоминать (0 — не напоминать заранее)\n"
    "/morning 08:30 — спросить про подъём, если он не отмечен (off — не спрашивать)"
)


def store(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.application.bot_data["storage"]


def tz_of(chat: Chat) -> ZoneInfo:
    return ZoneInfo(chat.tz)


def norm_of(chat: Chat, now: datetime) -> AgeNorm:
    return norm_for_age(age_in_months(chat.birth_date, now.date()))


def hm(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def plan_text(chat: Chat, plan: DayPlan, now: datetime) -> str:
    tz = tz_of(chat)
    months = age_in_months(chat.birth_date, now.date())
    norm = norm_for_age(months)
    lines = [f"📅 План на сегодня — {chat.child_name}, {format_age(months)}"]

    if plan.status == "night":
        lines.append(f"🌙 Ночной сон с {hm(plan.status_since, tz)}, "
                     f"идёт {format_duration(now - plan.status_since)}.")
        lines.append(f"Норма ночного сна в этом возрасте — {norm.night_hours} ч.")
        lines.append("Утром отметьте подъём — и я рассчитаю дневной сон и отбой.")
        return "\n".join(lines)
    if plan.morning_wake is None:
        lines.append("Утренний подъём не отмечен. Нажмите «☀️ Проснулись» "
                     "или напишите, например, «проснулся в 7:10».")
        return "\n".join(lines)

    lines.append(f"☀️ Подъём — {hm(plan.morning_wake.ts, tz)}")
    for i, nap in enumerate(plan.naps, 1):
        title = "Дневной сон" if len(plan.naps) == 1 else f"Сон {i}"
        if nap.skipped:
            lines.append(f"😴 {title} — пропущен (был в плане на {hm(nap.start, tz)})")
        elif nap.finished:
            lines.append(f"😴 {title} — {hm(nap.start, tz)}–{hm(nap.end, tz)} "
                         f"({format_duration(nap.end - nap.start)}) ✓")
        elif nap.started:
            lines.append(f"😴 {title} — спит с {hm(nap.start, tz)}, "
                         f"разбудить не позже {hm(nap.start + timedelta(hours=norm.nap_max), tz)}")
        else:
            lines.append(f"😴 {title} — {hm(nap.start, tz)}–{hm(nap.end, tz)} (план)")
    lines.append(f"🌙 Отбой — {hm(plan.bedtime, tz)}")

    if any(n.skipped for n in plan.naps):
        lines.append("\nДневной сон пропущен, поэтому отбой раньше обычного.")
    if plan.status == "awake":
        lines.append(f"\nСейчас бодрствует {format_duration(now - plan.status_since)}.")
    return "\n".join(lines)


def sessions(events: list[Event]) -> list[tuple[Event, datetime | None]]:
    """Пары «уснул → проснулся»."""
    out = []
    for i, e in enumerate(events):
        if e.kind in (NAP, NIGHT):
            wake = next((w for w in events[i + 1:] if w.kind == WAKE), None)
            nxt = next((w for w in events[i + 1:]), None)
            out.append((e, wake.ts if wake and wake is nxt else None))
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
    if event.kind == WAKE:
        prev = [e for e in events if e.ts < event.ts]
        head = f"☀️ Подъём в {hm(event.ts, tz)}"
        if prev and prev[-1].kind in (NAP, NIGHT):
            head += f" — спал(а) {format_duration(event.ts - prev[-1].ts)}"
    elif event.kind == NAP:
        head = f"😴 Дневной сон с {hm(event.ts, tz)}"
    else:
        head = f"🌙 Ночной сон с {hm(event.ts, tz)}. Спокойной ночи!"
    plan = build_plan(events, now, norm_of(chat, now))
    return head + "\n\n" + plan_text(chat, plan, now)


async def ensure_setup(update: Update, chat: Chat) -> bool:
    if chat.configured:
        return True
    await update.effective_message.reply_text(
        "Сначала расскажите о ребёнке: имя и дата рождения.\nНапример: Миша 12.03.2024"
    )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = store(context).chat(update.effective_chat.id)
    if not chat.configured:
        await update.effective_message.reply_text(
            "Привет! Я помогу следить за сном малыша: отмечайте подъёмы и засыпания, "
            "а я рассчитаю дневной сон и отбой по возрасту и напомню заранее.\n\n"
            "Напишите имя и дату рождения ребёнка, например: Миша 12.03.2024",
            reply_markup=KEYBOARD,
        )
        return
    await update.effective_message.reply_text(HELP, reply_markup=KEYBOARD)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, reply_markup=KEYBOARD)


async def set_child(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    birth = parse_birth_date(text)
    if not birth:
        return False
    name = re.sub(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}", "", text).strip(" ,") or "Малыш"
    s = store(context)
    chat_id = update.effective_chat.id
    s.update_chat(chat_id, child_name=name, birth_date=birth)
    chat = s.chat(chat_id)
    now = datetime.now(tz_of(chat))
    months = age_in_months(birth, now.date())
    norm = norm_for_age(months)
    windows = ", ".join(format_duration(timedelta(hours=w)) for w in norm.wake_windows)
    await update.effective_message.reply_text(
        f"Записал: {name}, {format_age(months)}.\n"
        f"Дневных снов: {len(norm.naps)}, окна бодрствования: {windows}, "
        f"ночной сон: {norm.night_hours} ч.\n"
        f"Часовой пояс: {chat.tz} (поменять — /tz).\n\n"
        "Теперь отмечайте подъём и засыпание кнопками внизу.",
        reply_markup=KEYBOARD,
    )
    return True


async def child_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await set_child(update, context, " ".join(context.args)):
        await update.effective_message.reply_text("Формат: /child Миша 12.03.2024")


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


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    if not await ensure_setup(update, chat):
        return
    now = datetime.now(tz_of(chat))
    plan = build_plan(s.recent(chat.chat_id, now), now, norm_of(chat, now))
    await update.effective_message.reply_text(plan_text(chat, plan, now), reply_markup=KEYBOARD)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    if await ensure_setup(update, chat):
        await update.effective_message.reply_text(today_text(chat, s, datetime.now(tz_of(chat))))


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    if await ensure_setup(update, chat):
        await update.effective_message.reply_text(week_text(chat, s, datetime.now(tz_of(chat))))


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


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text or ""
    s = store(context)
    chat = s.chat(update.effective_chat.id)
    if text == BTN_PLAN:
        await plan_cmd(update, context)
        return
    if text == BTN_TODAY:
        await today_cmd(update, context)
        return
    if not chat.configured:
        if not await set_child(update, context, text):
            await ensure_setup(update, chat)
        return

    tz = tz_of(chat)
    now = datetime.now(tz)
    parsed = parse_event(text, now)
    if not parsed:
        if update.effective_chat.type == "private":
            await update.effective_message.reply_text(
                "Не понял. Напишите, например, «проснулся в 7:10» или «уснула 5 минут назад». /help"
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
        event_text(chat, s, event, now), reply_markup=event_keyboard(event)
    )


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
            plan = build_plan(s.recent(chat.chat_id, now), now, norm)
            check = None
            if chat.morning_check:
                h, m = map(int, chat.morning_check.split(":"))
                check = now.replace(hour=h, minute=m, second=0, microsecond=0)
            for r in due_reminders(plan, now, tz, norm, timedelta(minutes=chat.remind_before),
                                   check, chat.child_name):
                if s.mark_sent(chat.chat_id, r.key):
                    await context.bot.send_message(chat.chat_id, r.text, reply_markup=KEYBOARD)
        except Exception:
            log.exception("Напоминание для чата %s не отправлено", chat.chat_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.bot_data["storage"] = Storage(os.environ.get("DB_PATH", "sleep.db"))

    app.add_handler(CommandHandler("start", start))
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(tick, interval=60, first=5)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
