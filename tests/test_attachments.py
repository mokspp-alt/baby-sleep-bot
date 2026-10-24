"""Фото и файлы: преобразование в блоки для Claude и путь от фото до записи сна."""

import asyncio
import base64
import io
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook
from PIL import Image

from sleepbot import attachments, bot
from sleepbot.assistant import Assistant, resolve_time
from sleepbot.schedule import NIGHT, WAKE
from sleepbot.storage import Storage

TZ = ZoneInfo("Europe/Moscow")


def png(width, height):
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, "PNG")
    return out.getvalue()


def test_small_image_kept_as_is():
    data = png(800, 600)
    block = attachments.to_block("фото", "image/png", data)
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png"
    assert base64.b64decode(block["source"]["data"]) == data


def test_big_image_downscaled():
    block = attachments.to_block("скрин.png", "image/png", png(5000, 3000))
    assert block["source"]["media_type"] == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(block["source"]["data"]))) as img:
        assert max(img.size) == attachments.MAX_SIDE


def test_pdf_text_and_excel():
    pdf = attachments.to_block("анализы.pdf", "application/pdf", b"%PDF-1.4 ...")
    assert pdf["source"]["media_type"] == "application/pdf" and pdf["title"] == "анализы.pdf"

    txt = attachments.to_block("сон.csv", None, "дата;подъём\n23.10;7:10".encode())
    assert txt["source"]["type"] == "text" and "7:10" in txt["source"]["data"]

    book = Workbook()
    book.active.title = "Октябрь"
    book.active.append(["Дата", "Подъём", "Отбой"])
    book.active.append(["23.10", "07:10", "20:30"])
    out = io.BytesIO()
    book.save(out)
    xlsx = attachments.to_block("дневник.xlsx", None, out.getvalue())
    assert "Октябрь" in xlsx["source"]["data"] and "23.10,07:10,20:30" in xlsx["source"]["data"]


def test_unsupported_and_broken_files():
    with pytest.raises(attachments.UnsupportedFile, match="Пока понимаю"):
        attachments.to_block("отчёт.docx", "application/msword", b"...")
    with pytest.raises(attachments.UnsupportedFile, match="картинку"):
        attachments.to_block("фото.jpg", "image/jpeg", b"not an image")


def test_dates_for_older_records():
    now = datetime(2026, 10, 24, 10, 0, tzinfo=TZ)
    assert resolve_time("07:10", "21.10.2026", now) == datetime(2026, 10, 21, 7, 10, tzinfo=TZ)
    assert resolve_time("07:10", "21.10", now) == datetime(2026, 10, 21, 7, 10, tzinfo=TZ)
    assert resolve_time("07:10", "31.12", now) == datetime(2025, 12, 31, 7, 10, tzinfo=TZ)
    with pytest.raises(ValueError, match="нет такой даты"):
        resolve_time("07:10", "31.02.2026", now)


def test_photo_to_records():
    s = Storage(":memory:")
    s.update_chat(1, child_name="Лёва", gender="m", birth_date=date(2024, 8, 1))
    calls = []
    usage = SimpleNamespace(input_tokens=1, output_tokens=1)

    def tool(id, name, **inp):
        return SimpleNamespace(type="tool_use", id=id, name=name, input=inp)

    responses = [
        ("tool_use", [tool("t1", "record_event", kind="night", time="21:00", day="21.10.2026"),
                      tool("t2", "record_event", kind="wake", time="07:10", day="22.10.2026")]),
        ("end_turn", [SimpleNamespace(type="text", text="Записал ночь с 21 на 22 октября.")]),
    ]

    async def create(**kw):
        calls.append({**kw, "messages": list(kw["messages"])})
        stop, content = responses.pop(0)
        return SimpleNamespace(stop_reason=stop, content=content, model=kw["model"], usage=usage)

    a = Assistant.__new__(Assistant)
    a.history, a.model, a.effort = {}, "claude-haiku-5-5", "medium"
    a.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))

    sent = []

    async def reply_text(t, reply_markup=None):
        sent.append(t)

    async def get_file():
        async def download_as_bytearray():
            return bytearray(png(1000, 800))
        return SimpleNamespace(download_as_bytearray=download_as_bytearray)

    async def send_chat_action(*args):
        pass

    photo = SimpleNamespace(file_size=50_000, get_file=get_file)
    message = SimpleNamespace(photo=[photo], document=None, caption="запиши сон из дневника",
                              reply_text=reply_text)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1, type="private"),
                             effective_message=message)
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=send_chat_action),
                              application=SimpleNamespace(bot_data={"storage": s, "assistant": a}))
    asyncio.run(bot.on_file(update, context))

    content = calls[0]["messages"][-1]["content"]
    assert content[0]["type"] == "image"
    assert "запиши сон из дневника" in content[-1]["text"]
    assert "Записал ночь" in sent[0]
    events = [(e.kind, e.ts.astimezone(TZ).strftime("%d.%m %H:%M"))
              for e in s.events(1, datetime(2000, 1, 1, tzinfo=TZ))]
    assert events == [(NIGHT, "21.10 21:00"), (WAKE, "22.10 07:10")]
    # В истории — пометка о фото, без самой картинки.
    assert a.history[1][0] == {"role": "user", "content": "[фото] запиши сон из дневника"}
