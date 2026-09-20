"""The chat plugin without SAP and without a model: protocol, service, handlers."""
import asyncio
import threading
import time

import pytest

from curie import config
from curie.chat.protocol import (Chunk, LineAssembler, WordChunker, messages_from_rows,
                                 messages_from_zchatmsg, to_sap)
from curie.chat.service import ChatError, ChatService
from curie.handlers.zbla_chat import (z_ai_chat_completion, z_bla_rfc_chat_close,
                                      z_bla_rfc_chat_open, z_bla_rfc_chat_read)
from curie.llm.client import LLMError
from curie.sapnwrfc720 import ABAPException

ATTRS = {"sysId": "TC0", "client": "005", "user": "THUPALO", "progName": "SAPMZBLA"}


def ctx():
    return {"function_name": "X", "connection_attributes": ATTRS}


class ScriptLLM:
    """Yields the given deltas; optional delay before each, optional failure at the end."""

    def __init__(self, deltas, delay=0.0, fail=None, finish="stop"):
        self.deltas = list(deltas)
        self.delay = delay
        self.fail = fail
        self.finish = finish
        self.seen = []

    async def stream(self, messages, tools=None, **kw):
        self.seen.append((messages, kw))
        for delta in self.deltas:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield ("delta", delta)
        if self.fail:
            raise LLMError(self.fail)
        yield ("done", {"content": "".join(self.deltas), "finish_reason": self.finish})


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop

    async def settle():
        # Streams still running (a slow fake model) are cancelled and awaited,
        # so no pending task is destroyed with the loop.
        tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run_coroutine_threadsafe(settle(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(2)
    loop.close()


@pytest.fixture(autouse=True)
def chat_config(monkeypatch):
    monkeypatch.setattr(config, "CHAT_PLUGIN", True)
    monkeypatch.setattr(config, "CHAT_MODELS", "ZGPT-45B=test-model,FAST=small")
    monkeypatch.setattr(config, "LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "CHAT_READ_WAIT", 0.5)
    monkeypatch.setattr(config, "CHAT_MAX_HANDLES", 4)
    monkeypatch.setattr(config, "CHAT_HANDLE_TTL", 300.0)
    monkeypatch.setattr(config, "CHAT_MAX_INPUT_CHARS", 60000)
    monkeypatch.setattr(config, "CHAT_SYNC_TIMEOUT", 5.0)
    monkeypatch.setattr(config, "CHAT_STREAM_THREADS", False)
    monkeypatch.setattr(config, "CHAT_SYSTEM_PROMPT", "System {sysid}/{client} in {language}.")
    monkeypatch.setattr(config, "LLM_ANSWER_LANGUAGE", "PL")


def drain(svc, handle, max_chunks=50, limit=5.0):
    """READ until done; returns all chunks."""
    out, deadline = [], time.monotonic() + limit
    while time.monotonic() < deadline:
        chunks, done = svc.read(handle, max_chunks)
        out.extend(chunks)
        if done:
            return out
    raise AssertionError("answer did not complete")


# -- protocol ----------------------------------------------------------------- #

def test_chunker_matches_the_specification_example():
    chunker = WordChunker()
    out = []
    for delta in ['Turn 1', ' - you', ' wrote:\n"hi"\n\n1. Send', ' the history.', '\n']:
        out.extend(chunker.feed(delta))
    out.extend(chunker.flush())
    assert [(c.text, c.newline) for c in out] == [
        ("Turn", False), ("1", False), ("-", False), ("you", False), ("wrote:", True),
        ('"hi"', True), ("", True), ("1.", False), ("Send", False), ("the", False),
        ("history.", True),
    ]


def test_chunker_holds_back_unfinished_word_and_splits_long_ones():
    chunker = WordChunker()
    assert chunker.feed("Hel") == []
    assert [c.text for c in chunker.feed("lo wor")] == ["Hello"]
    assert [c.text for c in chunker.flush()] == ["wor"]
    long = "x" * 450
    pieces = WordChunker().feed(long + " ")
    assert [len(c.text) for c in pieces] == [200, 200, 50]


def test_chunker_normalises_whitespace_and_control_characters():
    out = WordChunker().feed("a\t\tb\r\n\x07c  \x0cd\n")
    assert [(c.text, c.newline) for c in out] == [("a", False), ("b", True), ("c", False),
                                                  ("d", True)]


def test_assembler_is_the_inverse_of_the_chunker():
    text = "Turn 1 - you wrote:\n\"hi\"\n\n1. Send the history.\nlast line without break"
    chunker, assembler = WordChunker(), LineAssembler(255)
    for chunk in chunker.feed(text) + chunker.flush():
        assembler.feed(chunk)
    assembler.finish()
    assert assembler.text() == text


def test_assembler_wraps_before_the_overflowing_word_and_formats_tline():
    assembler = LineAssembler(width=12)
    for word in "one two three four".split():
        assembler.feed(Chunk(word))
    assembler.feed(Chunk("", newline=True))
    assembler.feed(Chunk("", newline=True))     # empty line
    assembler.feed(Chunk("end", newline=True))
    lines = assembler.finish()
    assert [(l.text, l.continuation) for l in lines] == [
        ("one two", False), ("three four", True), ("", False), ("end", False)]
    assert [l.tline() for l in lines] == [
        {"TDFORMAT": "*", "TDLINE": "one two"}, {"TDFORMAT": " ", "TDLINE": "three four"},
        {"TDFORMAT": "/", "TDLINE": ""}, {"TDFORMAT": "*", "TDLINE": "end"}]
    assert assembler.text() == "one two three four\n\nend"


def test_to_sap_transliterates_and_replaces_for_the_codepage():
    assert to_sap("a–b—c", "iso-8859-2") == "a-b-c"
    assert to_sap("zażółć", "iso-8859-2") == "zażółć"
    assert to_sap("日本", "iso-8859-2") == "??"
    assert len(to_sap("x" * 300, "iso-8859-2")) == 255
    assert Chunk("wrote:", newline=True).row("iso-8859-2") == {"TEXT": "wrote:", "NEWLINE": "X",
                                                                "DONE": " "}


def test_messages_from_rows_groups_lines_by_message():
    rows = [
        {"MSGNO": "0001", "ROLE": "user     ", "LINENO": "0001", "TEXT": "first line   "},
        {"MSGNO": "0001", "ROLE": "user", "LINENO": "0002", "TEXT": ""},
        {"MSGNO": "0001", "ROLE": "user", "LINENO": "0003", "TEXT": "third"},
        {"MSGNO": "0002", "ROLE": "assistant", "LINENO": "0001", "TEXT": "answer"},
        {"MSGNO": "0003", "ROLE": "USER", "LINENO": "0001", "TEXT": "next"},
    ]
    assert messages_from_rows(rows) == [
        {"role": "user", "content": "first line\n\nthird"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "next"},
    ]
    # Structures with other field names: positional fallback.
    assert messages_from_rows([{"A": "1", "B": "user", "C": "1", "D": "hi"}]) == [
        {"role": "user", "content": "hi"}]


def test_messages_from_zchatmsg_maps_roles_and_appends_the_prompt():
    rows = [{"MSGROL": "U", "MSGTXT": "hello  "}, {"MSGROL": "A", "MSGTXT": "hi"},
            {"MSGROL": "S", "MSGTXT": "be brief"}, {"MSGROL": "?", "MSGTXT": "odd"},
            {"MSGROL": "U", "MSGTXT": "   "}]
    assert messages_from_zchatmsg(rows, "next?") == [
        {"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"},
        {"role": "system", "content": "be brief"}, {"role": "user", "content": "odd"},
        {"role": "user", "content": "next?"}]


# -- service ------------------------------------------------------------------ #

def test_open_read_close_round_trip(loop):
    llm = ScriptLLM(["Hello", " wor", "ld.\nSecond line"])
    svc = ChatService(llm, loop)
    h = svc.open([{"role": "user", "content": "hi"}], caller="TC0 THUPALO", caller_attrs=ATTRS)
    assert h == 1
    chunks = drain(svc, h)
    assert [(c.text, c.newline, c.done) for c in chunks] == [
        ("Hello", False, False), ("world.", True, False), ("Second", False, False),
        ("line", False, False), ("", False, True)]
    assert svc.read(h, 5) == ([], True)          # after DONE: empty and done until CLOSE
    # The system prompt was prepended with the caller's system and client.
    messages, kw = llm.seen[0]
    assert messages[0] == {"role": "system", "content": "System TC0/005 in PL."}
    assert kw["model"] == "test-model"
    svc.close(h)
    with pytest.raises(ChatError) as exc:
        svc.read(h, 1)
    assert exc.value.key == "INVALID_HANDLE"
    svc.close(h)                                 # unknown handle: no-op
    assert svc.stats()["completed"] == 1 and svc.stats()["open"] == []


def test_read_returns_empty_while_nothing_has_arrived(loop):
    svc = ChatService(ScriptLLM(["late"], delay=0.4), loop)
    h = svc.open([{"role": "user", "content": "hi"}])
    started = time.monotonic()
    assert svc.read(h, 5, wait=0.1) == ([], False)
    assert time.monotonic() - started < 0.35
    assert [c.text for c in drain(svc, h)] == ["late", ""]


def test_error_after_partial_output_is_reported_on_the_next_read(loop):
    svc = ChatService(ScriptLLM(["Hello world ", "and"], fail="connection dropped"), loop)
    h = svc.open([{"role": "user", "content": "hi"}])
    time.sleep(0.2)
    chunks, done = svc.read(h, 50)
    assert [c.text for c in chunks] == ["Hello", "world", "and"] and done is False
    with pytest.raises(ChatError) as exc:
        svc.read(h, 50)
    assert exc.value.key == "SERVICE_ERROR" and "connection dropped" in exc.value.message
    assert svc.stats()["failed"] == 1


def test_truncated_marker_and_finish_reason(loop):
    svc = ChatService(ScriptLLM(["cut off"], finish="length"), loop)
    h = svc.open([{"role": "user", "content": "hi"}])
    assert [c.text for c in drain(svc, h)] == ["cut", "off", "[truncated]", ""]


def test_validation_and_model_resolution(loop):
    svc = ChatService(ScriptLLM(["x"]), loop)
    with pytest.raises(ChatError) as exc:
        svc.open([{"role": "assistant", "content": "only me"}])
    assert exc.value.key == "NO_USER_MESSAGE"
    with pytest.raises(ChatError) as exc:
        svc.open([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    assert exc.value.key == "NO_USER_MESSAGE"
    with pytest.raises(ChatError) as exc:
        svc.open([{"role": "user", "content": "a"}], model="NOPE")
    assert exc.value.key == "MODEL_NOT_AVAILABLE"
    assert svc.resolve_model("") == ("ZGPT-45B", "test-model")
    assert svc.resolve_model("fast") == ("FAST", "small")


def test_history_is_trimmed_to_the_input_limit(loop, monkeypatch):
    monkeypatch.setattr(config, "CHAT_MAX_INPUT_CHARS", 60)
    monkeypatch.setattr(config, "CHAT_SYSTEM_PROMPT", "sys")
    svc = ChatService(ScriptLLM(["x"]), loop)
    history = [{"role": "user", "content": "a" * 30}, {"role": "assistant", "content": "b" * 30},
               {"role": "user", "content": "c" * 30}, {"role": "assistant", "content": "d" * 10},
               {"role": "user", "content": "last"}]
    msgs = svc.prepare(history)
    # 3 + 30 + 30 + 30 + 10 + 4 = 107: the two oldest go, then 47 fits in 60.
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[-1]["content"] == "last" and msgs[1]["content"] == "c" * 30


def test_busy_limit_refuses_the_next_open(loop, monkeypatch):
    monkeypatch.setattr(config, "CHAT_MAX_HANDLES", 1)
    svc = ChatService(ScriptLLM(["slow"], delay=1.0), loop)
    h = svc.open([{"role": "user", "content": "hi"}])
    with pytest.raises(ChatError) as exc:
        svc.open([{"role": "user", "content": "hi"}])
    assert exc.value.key == "SERVICE_ERROR" and "busy" in exc.value.message
    svc.close(h)
    assert svc.open([{"role": "user", "content": "hi"}]) == 2
    assert svc.stats()["refused"] == 1


def test_expire_drops_handles_nobody_reads(loop, monkeypatch):
    svc = ChatService(ScriptLLM(["slow"], delay=1.0), loop)
    h = svc.open([{"role": "user", "content": "hi"}])
    assert svc.expire() == 0
    monkeypatch.setattr(config, "CHAT_HANDLE_TTL", 0.0)
    time.sleep(0.01)
    assert svc.expire() == 1
    with pytest.raises(ChatError) as exc:
        svc.read(h, 1)
    assert exc.value.key == "INVALID_HANDLE" and svc.stats()["expired"] == 1


def test_shutdown_wakes_a_waiting_read(loop):
    svc = ChatService(ScriptLLM(["never"], delay=5.0), loop)
    h = svc.open([{"role": "user", "content": "hi"}])
    result = {}

    def reader():
        try:
            svc.read(h, 1, wait=5.0)
            result["outcome"] = "returned"
        except ChatError as exc:
            result["outcome"] = exc.key

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.1)
    started = time.monotonic()
    svc.shutdown()
    thread.join(2)
    assert result["outcome"] == "SERVICE_ERROR" and time.monotonic() - started < 1.5
    with pytest.raises(ChatError):
        svc.open([{"role": "user", "content": "hi"}])


def test_thread_mode_uses_a_client_per_answer(loop, monkeypatch):
    monkeypatch.setattr(config, "CHAT_STREAM_THREADS", True)
    svc = ChatService(None, loop)
    svc.llm_factory = lambda: ScriptLLM(["from thread"])
    h = svc.open([{"role": "user", "content": "hi"}])
    assert [c.text for c in drain(svc, h)] == ["from", "thread", ""]
    svc.close(h)


def test_complete_returns_wrapped_lines_and_token_estimate(loop):
    svc = ChatService(ScriptLLM(["alpha beta gamma\n", "delta"]), loop)
    result = svc.complete([{"role": "user", "content": "q" * 40}], width=11)
    assert [(l.text, l.continuation) for l in result["lines"]] == [
        ("alpha beta", False), ("gamma", True), ("delta", False)]
    assert result["finish"] == "stop" and result["tokens"] > 0
    assert result["model"] == "ZGPT-45B" and svc.stats()["open"] == []


def test_complete_timeout_keeps_partial_output_or_raises(loop, monkeypatch):
    monkeypatch.setattr(config, "CHAT_READ_WAIT", 0.05)
    svc = ChatService(ScriptLLM(["first ", "never"], delay=0.3), loop)
    result = svc.complete([{"role": "user", "content": "hi"}], timeout=0.45)
    assert result["finish"] == "timeout"
    assert [l.text for l in result["lines"]] == ["first", "[timeout]"]
    svc = ChatService(ScriptLLM(["never"], delay=2.0), loop)
    with pytest.raises(ChatError) as exc:
        svc.complete([{"role": "user", "content": "hi"}], timeout=0.2)
    assert exc.value.key == "SERVICE_ERROR"


def test_complete_reports_a_stream_failure(loop):
    svc = ChatService(ScriptLLM(["some words"], fail="boom"), loop)
    result = svc.complete([{"role": "user", "content": "hi"}])
    assert result["finish"] == "error" and "boom" in result["error"]
    assert [l.text for l in result["lines"]] == ["some words", "[error]"]


# -- handlers ----------------------------------------------------------------- #

def test_handlers_streamed_path(loop):
    svc = ChatService(ScriptLLM(["Cześć –", " świecie\n"]), loop)
    rows = [{"MSGNO": "0001", "ROLE": "user", "LINENO": "0001", "TEXT": "hej"}]
    out = z_bla_rfc_chat_open(ctx(), IT_MESSAGES=rows)
    h = out["EV_HANDLE"]
    got, deadline = [], time.monotonic() + 5
    while time.monotonic() < deadline:
        r = z_bla_rfc_chat_read(ctx(), IV_HANDLE=h, IV_MAX_CHUNKS=10)
        got.extend(r["ET_CHUNKS"])
        if r["EV_DONE"] == "X":
            break
    assert got == [{"TEXT": "Cześć", "NEWLINE": " ", "DONE": " "},
                   {"TEXT": "-", "NEWLINE": " ", "DONE": " "},
                   {"TEXT": "świecie", "NEWLINE": "X", "DONE": " "},
                   {"TEXT": "", "NEWLINE": " ", "DONE": "X"}]
    assert z_bla_rfc_chat_close(ctx(), IV_HANDLE=h) == {}
    with pytest.raises(ABAPException) as exc:
        z_bla_rfc_chat_read(ctx(), IV_HANDLE=h)
    assert exc.value.key == "INVALID_HANDLE"
    with pytest.raises(ABAPException) as exc:
        z_bla_rfc_chat_open(ctx(), IT_MESSAGES=[])
    assert exc.value.key == "NO_USER_MESSAGE"
    assert svc.stats()["opened"] == 1


def test_handler_completion(loop):
    svc = ChatService(ScriptLLM(["Line one\nline two"]), loop)
    out = z_ai_chat_completion(ctx(), I_MODEL="fast", I_PROMPT="question", I_SESSID="S1",
                               IT_HISTORY=[{"MSGROL": "U", "MSGTXT": "earlier"},
                                           {"MSGROL": "A", "MSGTXT": "reply"}])
    assert out["ET_RESPONSE"] == [{"TDFORMAT": "*", "TDLINE": "Line one"},
                                  {"TDFORMAT": "*", "TDLINE": "line two"}]
    assert out["E_MODEL"] == "FAST" and out["E_TOKENS"] > 0
    messages = svc.llm.seen[0][0]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[-1]["content"] == "question"
    with pytest.raises(ABAPException) as exc:
        z_ai_chat_completion(ctx(), I_PROMPT="   ")
    assert exc.value.key == "EMPTY_PROMPT"
    with pytest.raises(ABAPException) as exc:
        z_ai_chat_completion(ctx(), I_MODEL="NOPE", I_PROMPT="q")
    assert exc.value.key == "MODEL_NOT_AVAILABLE"
