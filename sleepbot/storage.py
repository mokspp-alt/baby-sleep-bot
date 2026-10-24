"""SQLite: настройки чата, события сна, отправленные напоминания."""

from __future__ import annotations

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
    morning_check TEXT DEFAULT '08:30'
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_chat_ts ON events (chat_id, ts);
CREATE TABLE IF NOT EXISTS sent (
    chat_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);
"""


@dataclass
class Chat:
    chat_id: int
    child_name: str | None
    birth_date: date | None
    tz: str
    remind_before: int
    morning_check: str | None

    @property
    def configured(self) -> bool:
        return bool(self.child_name and self.birth_date)


def _utc(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def chat(self, chat_id: int) -> Chat:
        self.db.execute("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
        self.db.commit()
        r = self.db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return Chat(
            r["chat_id"], r["child_name"],
            date.fromisoformat(r["birth_date"]) if r["birth_date"] else None,
            r["tz"], r["remind_before"], r["morning_check"],
        )

    def chats(self) -> list[Chat]:
        ids = [r[0] for r in self.db.execute("SELECT chat_id FROM chats")]
        return [self.chat(i) for i in ids]

    def update_chat(self, chat_id: int, **fields) -> None:
        self.chat(chat_id)
        for name, value in fields.items():
            assert name in {"child_name", "birth_date", "tz", "remind_before", "morning_check"}
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

    def mark_sent(self, chat_id: int, key: str) -> bool:
        """True, если напоминание с этим ключом ещё не отправлялось."""
        cur = self.db.execute("INSERT OR IGNORE INTO sent (chat_id, key) VALUES (?, ?)", (chat_id, key))
        self.db.commit()
        return cur.rowcount == 1
