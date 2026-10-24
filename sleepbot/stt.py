"""Распознавание голосовых сообщений локальной моделью Whisper (faster-whisper).

Ключи и платные API не нужны. Модель скачивается с HuggingFace при первом голосовом
и кэшируется (в Docker — в /data/models). Размер задаётся WHISPER_MODEL:
tiny / base / small (по умолчанию, ~500 МБ, хорошо понимает русский) / medium.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

log = logging.getLogger(__name__)

# Подсказка модели: словарь и формат чисел, которые мы ждём («7:20», а не «семь двадцать»).
PROMPT = (
    "Проснулся в 7:20. Уснула в 13:05. Легли на ночь в 20:30. Уснул 10 минут назад. "
    "Когда дневной сон? Во сколько укладывать?"
)


class Transcriber:
    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                size = os.environ.get("WHISPER_MODEL", "small")
                root = os.environ.get("WHISPER_DIR") or None
                log.info("Загружаю модель Whisper %s", size)
                self._model = WhisperModel(size, device="cpu", compute_type="int8", download_root=root)
        return self._model

    def transcribe_file(self, path: str) -> str:
        segments, _ = self.load().transcribe(
            path, language="ru", beam_size=5, vad_filter=True, initial_prompt=PROMPT,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, path: str) -> str:
        return await asyncio.to_thread(self.transcribe_file, path)
