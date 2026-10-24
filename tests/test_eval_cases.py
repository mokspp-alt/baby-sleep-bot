"""Проверка самого сравнительного теста: эталонные ответы должны засчитываться,
а «ничего не делать» — нет (кроме случаев, где правильно ничего не менять)."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

import compare_models as cm  # noqa: E402
from sleepbot.assistant import Assistant  # noqa: E402


def scripted(responses):
    a = Assistant.__new__(Assistant)
    a.history, a.model, a.effort = {}, "claude-haiku-5-5", "low"
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0, cache_creation=None)

    async def create(**kw):
        stop, content = responses.pop(0)
        return SimpleNamespace(stop_reason=stop, content=content, model=kw["model"], usage=usage)

    a.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    return a


def gold_reply(case):
    """Ответ «идеальной модели»: эталонные вызовы, затем текст, подходящий под проверки."""
    calls = [SimpleNamespace(type="tool_use", id=f"t{i}", name=name, input=args)
             for i, (name, args) in enumerate(case.gold)]
    text = SimpleNamespace(type="text", text="Дневной сон в 12:30, отбой в 19:15, ночью спал 11 ч. "
                                             "Гулять зимой можно пару часов. Обратитесь к педиатру.")
    return ([("tool_use", calls)] if calls else []) + [("end_turn", [text])]


def test_gold_answers_pass():
    for case in cm.CASES:
        result = asyncio.run(cm.run_case(scripted(gold_reply(case)), case))
        assert result.ok, (case.id, result.why)
        assert result.cost > 0


def test_doing_nothing_fails_where_changes_expected():
    nothing = SimpleNamespace(type="text", text="Хорошо.")
    for case in cm.CASES:
        result = asyncio.run(cm.run_case(scripted([("end_turn", [nothing])]), case))
        should_pass = not case.gold and not case.reply
        assert result.ok == should_pass, case.id
