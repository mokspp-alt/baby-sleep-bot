"""Сравнение моделей Claude на задачах бота: понимает ли модель фразу и правильно ли меняет данные.

Каждый случай — реальная фраза родителя, исходные отметки и время «сейчас». Эталон — вызовы
инструментов, которые сделал бы идеальный помощник; правильность проверяется по итоговому
состоянию базы (отметки, назначенное время, настройки), а не по тексту вызовов. Для вопросов —
ещё и по ключевым словам в ответе. Тексты ответов всех моделей пишутся в файл для просмотра.

Запуск на сервере (ключ и прокси берутся из .env):
    .venv/bin/python evals/compare_models.py
    .venv/bin/python evals/compare_models.py claude-haiku-5-5:medium claude-sonnet-5-5:low
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sleepbot.assistant import Assistant  # noqa: E402
from sleepbot.bot import ChatTools  # noqa: E402
from sleepbot.schedule import NAP, NIGHT, WAKE  # noqa: E402
from sleepbot.storage import Storage  # noqa: E402

TZ = ZoneInfo("Europe/Moscow")
DEFAULT_CONFIGS = ["claude-sonnet-5-5:low", "claude-haiku-5-5:low", "claude-haiku-5-5:medium"]

# Цены за 1 млн токенов, $: ввод, вывод, чтение кэша, запись кэша 5 мин, запись кэша 1 ч.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50, 4.00),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00, 8.00),
}


def at(day: int, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


@dataclass
class Case:
    id: str
    message: str
    now: datetime
    events: list[tuple[str, datetime]] = field(default_factory=list)  # получат id 1, 2, 3…
    gold: list[tuple[str, dict]] = field(default_factory=list)  # эталонные вызовы инструментов
    reply: list[str] = field(default_factory=list)  # регулярки, которые должны быть в ответе
    setup: list[tuple[str, dict]] = field(default_factory=list)  # действия до сообщения


NIGHT_BEFORE = (NIGHT, at(23, "20:00"))
MORNING = (WAKE, at(24, "07:00"))

CASES = [
    # --- отметки ---
    Case("плохо спал", "Сегодня Лёва плохо спал и проснулся в 8:20", at(24, "09:00"),
         [(NIGHT, at(23, "21:30"))], [("record_event", dict(kind="wake", time="08:20", day="today"))]),
    Case("просто проснулся", "Проснулся", at(24, "07:15"), [NIGHT_BEFORE],
         [("record_event", dict(kind="wake", time="now", day="today"))]),
    Case("распознавание речи", "Лёва проснулся в семь двадцать пять", at(24, "07:40"), [NIGHT_BEFORE],
         [("record_event", dict(kind="wake", time="07:25", day="today"))]),
    Case("минут назад", "уснул минут 10 назад", at(24, "13:10"), [NIGHT_BEFORE, MORNING],
         [("record_event", dict(kind="sleep", time="13:00", day="today"))]),
    Case("в час дня", "Лёва уснул днём в час дня", at(24, "13:30"), [NIGHT_BEFORE, MORNING],
         [("record_event", dict(kind="nap", time="13:00", day="today"))]),
    Case("на ночь словами", "легли спать на ночь в половине девятого", at(24, "21:00"),
         [NIGHT_BEFORE, MORNING, (NAP, at(24, "12:30")), (WAKE, at(24, "14:30"))],
         [("record_event", dict(kind="night", time="20:30", day="today"))]),
    Case("вечером без уточнения", "уснул", at(24, "20:40"),
         [NIGHT_BEFORE, MORNING, (NAP, at(24, "12:30")), (WAKE, at(24, "14:30"))],
         [("record_event", dict(kind="sleep", time="now", day="today"))]),
    Case("два события сразу", "разбудили в 15:00, а уснул он в 13:10", at(24, "15:05"),
         [NIGHT_BEFORE, MORNING],
         [("record_event", dict(kind="nap", time="13:10", day="today")),
          ("record_event", dict(kind="wake", time="15:00", day="today"))]),
    Case("вчера и сегодня", "вчера уснул в 22:40, ночью пару раз просыпался, встал в 7:50",
         at(24, "08:10"), [],
         [("record_event", dict(kind="night", time="22:40", day="yesterday")),
          ("record_event", dict(kind="wake", time="07:50", day="today"))]),
    Case("ночное пробуждение", "ночью проснулся в 3 и плакал, потом снова уснул", at(24, "03:30"),
         [NIGHT_BEFORE], []),
    # --- исправления ---
    Case("поправить время", "ой, не в 8:20, а в 8:05", at(24, "08:30"),
         [NIGHT_BEFORE, (WAKE, at(24, "08:20"))],
         [("update_event", dict(event_id=2, kind="keep", time="08:05", day="today"))]),
    Case("поправить тип сна", "это был дневной сон, а не ночной", at(24, "16:40"),
         [NIGHT_BEFORE, MORNING, (NIGHT, at(24, "16:30"))],
         [("update_event", dict(event_id=3, kind="nap", time="keep", day="today"))]),
    Case("поправить вчерашнее", "вчера он уснул не в 20:00, а в 22:00", at(24, "09:00"),
         [NIGHT_BEFORE, MORNING],
         [("update_event", dict(event_id=1, kind="keep", time="22:00", day="yesterday"))]),
    Case("удалить лишнее", "удали последнюю отметку, я случайно нажал", at(24, "09:45"),
         [NIGHT_BEFORE, MORNING, (WAKE, at(24, "09:40"))], [("delete_event", dict(event_id=3))]),
    # --- время сна и настройки ---
    Case("отбой в гостях", "сегодня в гостях, уложим вечером в 21:30", at(24, "15:00"),
         [NIGHT_BEFORE, MORNING, (NAP, at(24, "12:30")), (WAKE, at(24, "14:20"))],
         [("set_plan_time", dict(target="bedtime", time="21:30"))]),
    Case("дневной сон на 13", "давай сегодня дневной сон в 13:00", at(24, "09:00"),
         [NIGHT_BEFORE, MORNING], [("set_plan_time", dict(target="nap", time="13:00"))]),
    Case("вернуть отбой", "верни отбой как было", at(24, "16:00"), [NIGHT_BEFORE, MORNING],
         [("set_plan_time", dict(target="bedtime", time="auto"))],
         setup=[("set_plan_time", dict(target="bedtime", time="21:30"))]),
    Case("напоминания", "напоминай за 15 минут", at(24, "10:00"), [NIGHT_BEFORE, MORNING],
         [("update_settings", dict(setting="remind_before_minutes", value="15"))]),
    Case("без дневного сна", "он уже не спит днём", at(24, "10:00"), [NIGHT_BEFORE, MORNING],
         [("update_settings", dict(setting="naps_per_day", value="0"))]),
    Case("переезд", "мы переехали в Новосибирск", at(24, "10:00"), [NIGHT_BEFORE, MORNING],
         [("update_settings", dict(setting="timezone", value="Asia/Novosibirsk"))]),
    # --- вопросы: ничего не меняем, отвечаем по плану ---
    Case("когда спать", "когда нам ложиться спать?", at(24, "10:00"), [NIGHT_BEFORE, MORNING],
         reply=[r"12[:.]30"]),
    Case("когда отбой", "во сколько сегодня отбой?", at(24, "10:00"), [NIGHT_BEFORE, MORNING],
         reply=[r"19[:.]15"]),
    Case("сколько спал", "сколько он спал ночью?", at(24, "10:00"), [NIGHT_BEFORE, MORNING],
         reply=[r"11"]),
    Case("не про сон", "сколько гулять с ребёнком зимой?", at(24, "10:00"), [NIGHT_BEFORE, MORNING],
         reply=[r"гул|прогул|мороз|холод|одева|час|минут"]),
    Case("температура", "у него температура 39 и он вялый", at(24, "10:00"), [NIGHT_BEFORE, MORNING],
         reply=[r"педиатр|врач|скор"]),
    Case("неточное время", "он проснулся то ли в семь, то ли в восемь, не помню", at(24, "09:00"),
         [NIGHT_BEFORE], []),
]


def fresh(case: Case) -> tuple[Storage, ChatTools]:
    s = Storage(":memory:")
    s.update_chat(1, child_name="Лёва", gender="m", birth_date=date(2024, 8, 1), tz="Europe/Moscow")
    for kind, ts in case.events:
        s.add_event(1, kind, ts)
    tools = ChatTools(s.chat(1), s, case.now)
    for name, args in case.setup:
        getattr(tools, name)(**args)
    tools.changed.clear()
    return s, ChatTools(s.chat(1), s, case.now)


def snapshot(s: Storage) -> tuple:
    chat = s.chat(1)
    events = sorted((e.kind, e.ts.astimezone(TZ).strftime("%d %H:%M"))
                    for e in s.events(1, datetime(2000, 1, 1, tzinfo=TZ)))
    overrides = sorted((target, ts.astimezone(TZ).strftime("%H:%M"))
                       for (_, target), ts in s.overrides(1).items())
    settings = (chat.child_name, chat.gender, chat.birth_date, chat.naps_pref, chat.wake_usual,
                chat.remind_before, chat.morning_check, chat.tz)
    return tuple(events), tuple(overrides), settings


def expected(case: Case) -> tuple:
    s, tools = fresh(case)
    for name, args in case.gold:
        getattr(tools, name)(**args)
    return snapshot(s)


def cost(model: str, usage) -> float:
    p_in, p_out, p_read, p_w5, p_w1h = PRICES.get(model, PRICES["claude-opus-5-5"])
    creation = getattr(usage, "cache_creation", None)
    w1h = getattr(creation, "ephemeral_1h_input_tokens", 0) or 0 if creation else 0
    w5 = (getattr(usage, "cache_creation_input_tokens", 0) or 0) - w1h
    return ((usage.input_tokens or 0) * p_in + (usage.output_tokens or 0) * p_out
            + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p_read
            + w5 * p_w5 + w1h * p_w1h) / 1_000_000


@dataclass
class Result:
    case: Case
    ok: bool
    why: str
    reply: str
    cost: float
    seconds: float


async def run_case(assistant: Assistant, case: Case) -> Result:
    """assistant — свой на каждый случай: подменяем его create, чтобы посчитать стоимость."""
    s, tools = fresh(case)
    spent = []
    create = assistant.client.beta.messages.create

    async def counting_create(**kw):
        response = await create(**kw)
        spent.append(cost(kw["model"], response.usage))
        return response

    assistant.client.beta.messages.create = counting_create
    started = time.monotonic()
    try:
        reply = await assistant.handle(1, case.message, tools.state(), tools)
    except Exception as e:
        return Result(case, False, f"ошибка: {e!r}", "", sum(spent), time.monotonic() - started)
    seconds = time.monotonic() - started

    got, want = snapshot(s), expected(case)
    problems = []
    if got[0] != want[0]:
        problems.append(f"отметки {list(got[0])} вместо {list(want[0])}")
    if got[1] != want[1]:
        problems.append(f"время {list(got[1])} вместо {list(want[1])}")
    if got[2] != want[2]:
        problems.append(f"настройки {got[2]} вместо {want[2]}")
    for pattern in case.reply:
        if not re.search(pattern, reply, re.I):
            problems.append(f"в ответе нет /{pattern}/")
    return Result(case, not problems, "; ".join(problems), reply, sum(spent), seconds)


async def run_config(config: str, key: str, proxy: str | None) -> list[Result]:
    model, _, effort = config.partition(":")
    semaphore = asyncio.Semaphore(4)

    async def one(case: Case) -> Result:
        async with semaphore:
            return await run_case(Assistant(key, proxy, model=model, effort=effort or "low"), case)

    # Первый случай отдельно: он записывает кэш инструкций, остальные его читают.
    first = await one(CASES[0])
    rest = await asyncio.gather(*(one(c) for c in CASES[1:]))
    return [first, *rest]


def load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


async def main() -> None:
    load_env(ROOT / ".env")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("Нет ANTHROPIC_API_KEY в .env")
    proxy = os.environ.get("ANTHROPIC_PROXY") or os.environ.get("TELEGRAM_PROXY") or None
    configs = sys.argv[1:] or DEFAULT_CONFIGS
    print(f"Случаев: {len(CASES)}, конфигураций: {len(configs)}. Это займёт пару минут.\n")

    all_results = {}
    for config in configs:
        all_results[config] = await run_config(config, key, proxy)
        results = all_results[config]
        passed = sum(r.ok for r in results)
        print(f"{config:26} верно {passed}/{len(results)}  "
              f"${sum(r.cost for r in results) / len(results):.4f} за сообщение  "
              f"{sum(r.seconds for r in results) / len(results):.1f} с")

    print("\nОшибки:")
    for config, results in all_results.items():
        for r in results:
            if not r.ok:
                print(f"- {config} / {r.case.id}: {r.why}")

    out = ROOT / "evals" / f"results-{datetime.now():%Y%m%d-%H%M}.txt"
    with out.open("w", encoding="utf-8") as f:
        for case_index, case in enumerate(CASES):
            f.write(f"\n=== {case.id}: «{case.message}»\n")
            for config, results in all_results.items():
                r = results[case_index]
                f.write(f"[{config}] {'OK' if r.ok else 'ОШИБКА: ' + r.why}\n{r.reply}\n")
    print(f"\nВсе ответы моделей: {out}")


if __name__ == "__main__":
    asyncio.run(main())
