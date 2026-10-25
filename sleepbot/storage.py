"""SQLite: настройки чата, события сна, отправленные напоминания."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .schedule import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    child_name TEXT,
    birth_date TEXT,
    tz TEXT NOT NULL DEFAULT 'Europe/Moscow',
    remind_before INTEGER NOT NULL DEFAULT 30,
    morning_check TEXT DEFAULT '08:30',
    gender TEXT,
    naps_pref INTEGER,
    wake_usual TEXT,
    setup_step TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_chat_ts ON events (chat_id, ts);
CREATE TABLE IF NOT EXISTS overrides (
    chat_id INTEGER NOT NULL,
    day_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    ts TEXT NOT NULL,
    PRIMARY KEY (chat_id, day_id, target)
);
CREATE TABLE IF NOT EXISTS feedback (
    chat_id INTEGER NOT NULL,
    sleep_id INTEGER NOT NULL,
    mood TEXT,
    latency INTEGER,
    wakings INTEGER,
    PRIMARY KEY (chat_id, sleep_id)
);
CREATE TABLE IF NOT EXISTS unusual_days (
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (chat_id, day)
);
CREATE TABLE IF NOT EXISTS sent (
    chat_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);
"""

# Колонки, добавленные после первой версии, — для баз, созданных раньше.
MIGRATIONS = {
    "gender": "TEXT", "naps_pref": "INTEGER", "wake_usual": "TEXT", "setup_step": "TEXT",
    "adj_nap": "INTEGER NOT NULL DEFAULT 0", "adj_bed": "INTEGER NOT NULL DEFAULT 0",
    "last_review": "TEXT", "pending": "TEXT",
}
FIELDS = {"child_name", "birth_date", "tz", "remind_before", "morning_check", *MIGRATIONS}


@dataclass
class Chat:
    chat_id: int
    child_name: str | None
    birth_date: date | None
    tz: str
    remind_before: int
    morning_check: str | None
    gender: str | None = None  # "m" / "f"
    naps_pref: int | None = None  # сколько раз спит днём по словам родителей
    wake_usual: str | None = None  # обычный подъём, "07:00"
    setup_step: str | None = None  # текущий вопрос анкеты
    adj_nap: int = 0  # личная поправка окна бодрствования перед дневным сном, минуты
    adj_bed: int = 0  # личная поправка окна перед ночным сном, минуты
    last_review: str | None = None  # дата последнего еженедельного разбора
    pending: str | None = None  # предложение разбора, ждущее ответа (JSON)

    @property
    def configured(self) -> bool:
        return bool(self.child_name and self.birth_date) and self.setup_step is None


def _utc(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str) -> None:
        if path != ":memory:" and os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(chats)")}
        for name, sql in MIGRATIONS.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE chats ADD COLUMN {name} {sql}")
        self.db.commit()

    def chat(self, chat_id: int) -> Chat:
        self.db.execute("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
        self.db.commit()
        r = self.db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return Chat(
            r["chat_id"], r["child_name"],
            date.fromisoformat(r["birth_date"]) if r["birth_date"] else None,
            r["tz"], r["remind_before"], r["morning_check"],
            r["gender"], r["naps_pref"], r["wake_usual"], r["setup_step"],
            r["adj_nap"], r["adj_bed"], r["last_review"], r["pending"],
        )

    def chats(self) -> list[Chat]:
        ids = [r[0] for r in self.db.execute("SELECT chat_id FROM chats")]
        return [self.chat(i) for i in ids]

    def update_chat(self, chat_id: int, **fields) -> None:
        self.chat(chat_id)
        for name, value in fields.items():
            assert name in FIELDS
            if isinstance(value, date):
                value = value.isoformat()
            self.db.execute(f"UPDATE chats SET {name} = ? WHERE chat_id = ?", (value, chat_id))
        self.db.commit()

    def add_event(self, chat_id: int, kind: str, ts: datetime) -> int:
        cur = self.db.execute(
            "INSERT INTO events (chat_id, kind, ts) VALUES (?, ?, ?)", (chat_id, kind, _utc(ts))
        )
        self.db.commit()
        return cur.lastrowid

    def event(self, chat_id: int, event_id: int) -> Event | None:
        r = self.db.execute(
            "SELECT * FROM events WHERE chat_id = ? AND id = ?", (chat_id, event_id)
        ).fetchone()
        return Event(r["id"], r["kind"], datetime.fromisoformat(r["ts"])) if r else None

    def update_event(self, chat_id: int, event_id: int, kind: str | None = None,
                     ts: datetime | None = None) -> None:
        if kind:
            self.db.execute("UPDATE events SET kind = ? WHERE chat_id = ? AND id = ?",
                            (kind, chat_id, event_id))
        if ts:
            self.db.execute("UPDATE events SET ts = ? WHERE chat_id = ? AND id = ?",
                            (_utc(ts), chat_id, event_id))
        self.db.commit()

    def delete_event(self, chat_id: int, event_id: int) -> None:
        self.db.execute("DELETE FROM events WHERE chat_id = ? AND id = ?", (chat_id, event_id))
        self.db.commit()

    def last_event(self, chat_id: int) -> Event | None:
        events = self.events(chat_id, since=datetime.min.replace(tzinfo=timezone.utc), limit=1)
        return events[-1] if events else None

    def events(self, chat_id: int, since: datetime, limit: int = 500) -> list[Event]:
        rows = self.db.execute(
            "SELECT * FROM events WHERE chat_id = ? AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (chat_id, _utc(since), limit),
        ).fetchall()
        return [Event(r["id"], r["kind"], datetime.fromisoformat(r["ts"])) for r in reversed(rows)]

    def recent(self, chat_id: int, now: datetime) -> list[Event]:
        """События за последние двое суток — этого хватает для плана текущего дня."""
        return self.events(chat_id, since=now - timedelta(hours=48))

    def set_override(self, chat_id: int, day_id: int, target: str, ts: datetime | None) -> None:
        """Время сна, назначенное родителями на день; None — вернуть расчётное."""
        if ts is None:
            self.db.execute("DELETE FROM overrides WHERE chat_id = ? AND day_id = ? AND target = ?",
                            (chat_id, day_id, target))
        else:
            self.db.execute("INSERT OR REPLACE INTO overrides VALUES (?, ?, ?, ?)",
                            (chat_id, day_id, target, _utc(ts)))
        self.db.commit()

    def overrides(self, chat_id: int) -> dict:
        rows = self.db.execute("SELECT day_id, target, ts FROM overrides WHERE chat_id = ?", (chat_id,))
        return {(r["day_id"], r["target"]): datetime.fromisoformat(r["ts"]) for r in rows}

    def set_feedback(self, chat_id: int, sleep_id: int, **fields) -> None:
        """mood ("good" / "ok" / "bad"), latency (минут до засыпания), wakings (ночных пробуждений)."""
        self.db.execute("INSERT OR IGNORE INTO feedback (chat_id, sleep_id) VALUES (?, ?)",
                        (chat_id, sleep_id))
        for name, value in fields.items():
            assert name in {"mood", "latency", "wakings"}
            self.db.execute(f"UPDATE feedback SET {name} = ? WHERE chat_id = ? AND sleep_id = ?",
                            (value, chat_id, sleep_id))
        self.db.commit()

    def feedback(self, chat_id: int) -> dict[int, dict]:
        rows = self.db.execute("SELECT * FROM feedback WHERE chat_id = ?", (chat_id,))
        return {r["sleep_id"]: {"mood": r["mood"], "latency": r["latency"], "wakings": r["wakings"]}
                for r in rows}

    def set_unusual_day(self, chat_id: int, day: date, reason: str | None) -> None:
        """Особый день (болезнь, зубы, поездка) — не учитывать в выводах. reason=None — снять."""
        if reason is None:
            self.db.execute("DELETE FROM unusual_days WHERE chat_id = ? AND day = ?",
                            (chat_id, day.isoformat()))
        else:
            self.db.execute("INSERT OR REPLACE INTO unusual_days VALUES (?, ?, ?)",
                            (chat_id, day.isoformat(), reason))
        self.db.commit()

    def unusual_days(self, chat_id: int) -> dict[date, str]:
        rows = self.db.execute("SELECT day, reason FROM unusual_days WHERE chat_id = ?", (chat_id,))
        return {date.fromisoformat(r["day"]): r["reason"] for r in rows}

    def mark_sent(self, chat_id: int, key: str) -> bool:
        """True, если напоминание с этим ключом ещё не отправлялось."""
        cur = self.db.execute("INSERT OR IGNORE INTO sent (chat_id, key) VALUES (?, ?)", (chat_id, key))
        self.db.commit()
        return cur.rowcount == 1
