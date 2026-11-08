"""Повторные попытки для запросов к Telegram.

Прокси до api.telegram.org иногда «зависает»: соединение устанавливается,
а ответ не приходит. Без повтора такой таймаут съедает готовый ответ —
человек видит «Что-то пошло не так», хотя отметка уже записана.

Оборачиваем ``Bot._do_post``: через него идут все вызовы Bot API (сообщения,
правки, файлы, getUpdates), поэтому повторяются и ответы, и напоминания.

Повторяем только сетевые ошибки (:class:`telegram.error.NetworkError`,
включая его наследника ``TimedOut``). Ошибки валидации и лимиты пробрасываем
сразу: повтор их не исправит.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram.error import NetworkError

log = logging.getLogger(__name__)

ATTEMPTS = 3  # всего попыток, включая первую
PAUSES = (1.0, 3.0)  # пауза перед 2-й и 3-й попыткой, секунды


def with_retries(bot: Any, attempts: int = ATTEMPTS,
                 pauses: tuple[float, ...] = PAUSES) -> None:
    """Заменяет ``bot._do_post`` версией, которая повторяет сетевые ошибки.

    Если первая отправка всё-таки дошла, а ответ потерялся, повтор может
    продублировать сообщение — это плата за то, что ответ не пропадёт.
    """
    original = bot._do_post

    async def _do_post(endpoint: str, data: Any, **kwargs: Any) -> Any:
        for attempt in range(attempts):
            try:
                return await original(endpoint, data, **kwargs)
            except NetworkError as exc:
                if attempt == attempts - 1:
                    raise
                pause = pauses[min(attempt, len(pauses) - 1)] if pauses else 0.0
                log.warning("Telegram %s: %s — повтор %d/%d через %.1f с",
                            endpoint, exc, attempt + 1, attempts - 1, pause)
                await asyncio.sleep(pause)

    bot._do_post = _do_post
