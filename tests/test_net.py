import asyncio

from telegram.error import BadRequest, NetworkError, TimedOut

from sleepbot import net
from sleepbot.bot import test_chat_ids


class FakeBot:
    """Бот, у которого первые ``failures`` вызовов падают заданной ошибкой."""

    def __init__(self, failures=0, error=None):
        self.failures = failures
        self.error = error or TimedOut()
        self.calls = 0

    async def _do_post(self, endpoint, data, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return {"ok": True, "endpoint": endpoint}


def run(bot):
    net.with_retries(bot, attempts=3, pauses=(0, 0))
    return asyncio.run(bot._do_post("sendMessage", {"text": "привет"}))


def test_retry_after_timeout():
    bot = FakeBot(failures=1)
    assert run(bot)["ok"] is True
    assert bot.calls == 2


def test_retry_after_network_error():
    bot = FakeBot(failures=2, error=NetworkError("connection reset"))
    assert run(bot)["ok"] is True
    assert bot.calls == 3


def test_gives_up_after_last_attempt():
    bot = FakeBot(failures=5)
    try:
        run(bot)
    except NetworkError:
        pass
    else:
        raise AssertionError("после трёх попыток ошибка должна проброситься")
    assert bot.calls == 3


def test_validation_error_is_not_retried():
    bot = FakeBot(failures=1, error=BadRequest("chat not found"))
    try:
        run(bot)
    except BadRequest:
        pass
    else:
        raise AssertionError("BadRequest не повторяем")
    assert bot.calls == 1


def test_test_chat_ids():
    assert test_chat_ids("") == set()
    assert test_chat_ids("  ") == set()
    assert test_chat_ids("-5554529018, 471946185") == {-5554529018, 471946185}
    assert test_chat_ids("-1,abc,2") == {-1, 2}
