import asyncio
import os
from datetime import date

from sleepbot import bot
from sleepbot.storage import Chat


class FakeStorage:
    def __init__(self, chats):
        self._chats = chats

    def chats(self):
        return self._chats

    def recent(self, *args, **kwargs):
        return []


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(chat_id)


class FakeContext:
    def __init__(self, storage, bot_):
        self.application = type("App", (), {"bot_data": {"storage": storage}})()
        self.bot = bot_


def chat(chat_id):
    return Chat(chat_id=chat_id, child_name="Лева", birth_date=date(2024, 10, 27),
                tz="Europe/Moscow", remind_before=30, morning_check=None)


def test_test_chat_gets_no_reminders(monkeypatch):
    monkeypatch.setenv("TEST_CHAT_IDS", "-5554529018,471946185")
    storage = FakeStorage([chat(-5554529018), chat(471946185)])
    fake_bot = FakeBot()
    asyncio.run(bot.tick(FakeContext(storage, fake_bot)))
    assert fake_bot.sent == []
