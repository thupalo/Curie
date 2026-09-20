"""The turn loop and the code path with a fake model, fake tools and a fake SAP."""
import asyncio
import json

from curie import config
from curie.llm.abap_flow import AbapFlow
from curie.llm.orchestrator import Orchestrator, trim_history
from curie.sessions.manager import SessionManager
from curie.sessions.model import State

ATTRS = {"sysId": "R45", "client": "100", "user": "HUPALO", "partnerRel": "45B"}


class FakeLLM:
    """Scripted answers: each item is (text, tool_calls) for stream(); complete() replies audit."""

    def __init__(self, answers, audit='{"allowed": true, "risk": "low", "violations": [], "summary": "ok"}'):
        self.answers = list(answers)
        self.audit = audit
        self.seen = []

    async def stream(self, messages, tools=None, **kw):
        self.seen.append(messages)
        text, calls = self.answers.pop(0)
        for piece in text.split(" "):
            yield ("delta", piece + " ")
            await asyncio.sleep(0)
        if calls:
            yield ("tool_calls", calls)
        yield ("done", {"content": text, "finish_reason": "stop"})

    async def complete(self, messages, **kw):
        return self.audit


class FakeToolBox:
    schemas = []

    @staticmethod
    def tables_in(name, args):
        return [str(args["table"]).upper()] if args.get("table") else []

    def describe(self, name, args):
        return f"{name} {args}"

    async def dispatch(self, name, args):
        return True, {"rows": [[1]], "note": ""}, "1 row"


class FakeExecutor:
    def __init__(self, syntax_ok=True, lines=("Hello", "World")):
        self.syntax_ok = syntax_ok
        self.lines = list(lines)
        self.checked = []
        self.executed = []

    def effective_width(self):
        return 72

    async def check(self, session, run, lines):
        self.checked.append(lines)
        if self.syntax_ok is True or (isinstance(self.syntax_ok, list) and self.syntax_ok.pop(0)):
            return {"ok": True, "message": "", "line": 0, "word": ""}
        return {"ok": False, "message": "Field LV_X is unknown", "line": 3, "word": "LV_X"}

    async def execute(self, session, run, lines, on_started=None):
        self.executed.append(run.progname)
        run.job = "ZAI_X/123"
        run.mode = "job"
        if on_started:
            on_started(run)
        if self.lines == ["<hang>"]:
            await asyncio.sleep(10)
        return {"ok": True, "rc": 0, "message": "", "lines": self.lines}

    async def abort(self, run):
        self.aborted = getattr(self, "aborted", []) + [run.job]
        return f"job {run.job} aborted"


def setup(tmp_path, llm, executor=None):
    mgr = SessionManager(public_url="http://c", log_dir=str(tmp_path))
    mgr.bind_loop(asyncio.get_running_loop())
    orch = Orchestrator(mgr, llm, FakeToolBox())
    flow = AbapFlow(mgr, executor or FakeExecutor(), llm, orch)
    orch.flow = flow
    session = mgr.open_from_rfc(ATTRS, "")
    mgr.touch_browser(session)
    q = mgr.subscribe(session)
    while not q.empty():
        q.get_nowait()
    return mgr, orch, flow, session, q


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_turn_with_tool_call_and_plain_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)

    async def scenario():
        llm = FakeLLM([
            ("", [{"id": "c1", "name": "sap_read_table", "arguments": json.dumps({"table": "T000"})}]),
            ("There are 3 clients.", None),
        ])
        mgr, orch, flow, session, q = setup(tmp_path, llm)
        assert orch.start_turn(session, "how many clients?")
        assert not orch.start_turn(session, "again")  # busy
        await session.turn_task
        kinds = [e for e, _ in drain(q)]
        assert kinds[0] == "user_message"
        assert "tool_call" in kinds and "tool_result" in kinds
        assert kinds[-2:] == ["assistant_done", "state"]
        assert session.state is State.IDLE
        roles = [m["role"] for m in session.messages]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert session.messages[2]["tool_call_id"] == "c1"
        # the second call to the model saw the tool result
        assert any(m.get("role") == "tool" for m in llm.seen[1])

    asyncio.run(scenario())


def test_code_path_confirm_and_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_INTERPRET_RESULT", True)
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    program = "```abap\nREPORT zai_temp.\nSELECT SINGLE * FROM t000 INTO wa.\nWRITE: / 'hi'.\n```"

    async def scenario():
        llm = FakeLLM([("Here is a program:\n" + program, None),
                       ("The program printed hi.", None)])
        ex = FakeExecutor()
        mgr, orch, flow, session, q = setup(tmp_path, llm, ex)
        orch.start_turn(session, "print hi")
        await session.turn_task
        events = drain(q)
        code_ready = [d for e, d in events if e == "code_ready"]
        assert len(code_ready) == 1 and code_ready[0]["can_run"] and code_ready[0]["syntax"]["ok"]
        assert code_ready[0]["audit"]["allowed"] is True
        assert code_ready[0]["source"].startswith("REPORT zai_")
        assert session.state is State.CONFIRM
        run_id = code_ready[0]["run_id"]

        assert flow.confirm(session, run_id)
        await session.turn_task
        events = drain(q)
        kinds = [e for e, _ in events]
        assert "run_started" in kinds and "run_result" in kinds
        result = [d for e, d in events if e == "run_result"][0]
        assert result["ok"] and result["lines"] == ["Hello", "World"]
        assert ex.executed == [code_ready[0]["progname"]]
        # the list was fed back and the model answered
        assert [d for e, d in events if e == "assistant_done"][-1]["text"] == "The program printed hi."
        assert session.state is State.IDLE
        assert session.runs[run_id].status == "done"

    asyncio.run(scenario())


def test_syntax_error_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)
    monkeypatch.setattr(config, "ABAP_FIX_RETRIES", 2)
    bad = "```abap\nWRITE lv_x.\n```"
    good = "```abap\nWRITE 'x'.\n```"

    async def scenario():
        llm = FakeLLM([("try this " + bad, None), ("fixed " + good, None)])
        ex = FakeExecutor(syntax_ok=[False, True])
        mgr, orch, flow, session, q = setup(tmp_path, llm, ex)
        orch.start_turn(session, "go")
        await session.turn_task
        events = drain(q)
        kinds = [e for e, _ in events]
        assert kinds.count("syntax_error") == 1
        assert kinds.count("code_ready") == 2
        assert [d for e, d in events if e == "code_ready"][-1]["can_run"]
        # the fix request went to the model as a hidden user message
        fix = llm.seen[1][-1]
        assert fix["role"] == "user" and "line 3" in fix["content"] and "LV_X" in fix["content"]
        assert kinds.count("user_message") == 1
        assert session.state is State.CONFIRM

    asyncio.run(scenario())


def test_static_rules_block_and_retry_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)
    monkeypatch.setattr(config, "ABAP_FIX_RETRIES", 1)
    bad = "```abap\nDELETE FROM mara WHERE matnr = 'X'.\n```"

    async def scenario():
        llm = FakeLLM([(bad, None), (bad, None), (bad, None)])
        mgr, orch, flow, session, q = setup(tmp_path, llm)
        orch.start_turn(session, "delete")
        await session.turn_task
        events = drain(q)
        ready = [d for e, d in events if e == "code_ready"]
        assert len(ready) == 2 and not any(r["can_run"] for r in ready)
        assert any("DELETE" in v for v in ready[0]["audit"]["violations"])
        notices = [d["text"] for e, d in events if e == "notice"]
        assert any("attempts" in n for n in notices)
        assert session.state is State.IDLE

    asyncio.run(scenario())


def test_stop_cancels_stream(tmp_path):
    class SlowLLM(FakeLLM):
        async def stream(self, messages, tools=None, **kw):
            yield ("delta", "partial ")
            await asyncio.sleep(10)
            yield ("done", {"content": "never"})

    async def scenario():
        mgr, orch, flow, session, q = setup(tmp_path, SlowLLM([]))
        orch.start_turn(session, "long")
        await asyncio.sleep(0.05)
        assert orch.stop(session)
        await asyncio.sleep(0.05)
        done = [d for e, d in drain(q) if e == "assistant_done"]
        assert done and done[-1]["interrupted"] and done[-1]["text"] == "partial"
        assert session.state is State.IDLE
        assert "[answer interrupted" in session.messages[-1]["content"]

    asyncio.run(scenario())


def test_trim_history_keeps_tool_groups_together():
    msgs = [{"role": "user", "content": "a" * 1000}]
    for i in range(5):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [{"id": str(i)}]})
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": "r" * 1000})
        msgs.append({"role": "assistant", "content": "x" * 1000})
    kept = trim_history(msgs, max_tokens=900)  # 3600 chars budget
    assert kept[0]["role"] != "tool"
    assert kept[-1] is msgs[-1]
    assert len(kept) < len(msgs)


def test_stop_while_running_aborts_the_job(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)
    program = "```abap\nWRITE 'hi'.\n```"

    async def scenario():
        llm = FakeLLM([(program, None)])
        ex = FakeExecutor(lines=["<hang>"])
        mgr, orch, flow, session, q = setup(tmp_path, llm, ex)
        orch.start_turn(session, "go")
        await session.turn_task
        run_id = [d for e, d in drain(q) if e == "code_ready"][0]["run_id"]
        assert flow.confirm(session, run_id)
        await asyncio.sleep(0.05)
        assert session.state is State.RUNNING
        assert orch.stop(session) is True
        await asyncio.sleep(0.05)
        assert ex.aborted == ["ZAI_X/123"]
        result = [d for e, d in drain(q) if e == "run_result"][-1]
        assert result["ok"] is False and "Stopped by the user" in result["message"]
        assert session.runs[run_id].status == "aborted"
        assert session.state is State.IDLE

    asyncio.run(scenario())


# -- the light path for general questions ------------------------------------------ #

class RecordingLLM(FakeLLM):
    def __init__(self, answers):
        super().__init__(answers)
        self.kwargs = []

    async def stream(self, messages, tools=None, **kw):
        self.kwargs.append({"tools": tools, **kw})
        async for item in super().stream(messages, tools=tools, **kw):
            yield item


def test_general_question_takes_the_light_path_until_data_is_named(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_LIGHT_MAX_TOKENS", 1234)

    async def scenario():
        llm = RecordingLLM([("Użyj FD10 i FBL5.", None), ("W 4.5B: FD10.", None), ("Saldo klienta: 12.", None)])
        mgr, orch, flow, session, q = setup(tmp_path, llm)
        orch.start_turn(session, "Pokaż mi jak wyświetlić saldo klienta w FI za pomocą SAP GUI")
        await session.turn_task
        assert session.thread_light and llm.kwargs[0]["tools"] is None and llm.kwargs[0]["max_tokens"] == 1234
        assert "This question is about SAP in general" in llm.seen[0][0]["content"]
        # A follow-up about the same general topic stays light.
        orch.start_turn(session, "Skoryguj powyższe wskazówki, używam SAP R/3 4.5B")
        await session.turn_task
        assert session.thread_light and llm.kwargs[1]["tools"] is None and len(session.thread) == 2
        # A follow-up that names data leaves the light path: tools and the full budget again.
        orch.start_turn(session, "A teraz pokaż to samo dla umowy 100/2005")
        await session.turn_task
        assert not session.thread_light and llm.kwargs[2]["tools"] is not None
        assert llm.kwargs[2]["max_tokens"] != 1234
        assert "This question is about SAP in general" not in llm.seen[2][0]["content"]

    asyncio.run(scenario())


def test_light_path_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_LIGHT_PATH", False)

    async def scenario():
        llm = RecordingLLM([("Użyj FD10.", None)])
        mgr, orch, flow, session, q = setup(tmp_path, llm)
        orch.start_turn(session, "Who are you?")
        await session.turn_task
        assert not session.thread_light and llm.kwargs[0]["tools"] is not None

    asyncio.run(scenario())


def test_light_path_uses_the_facades_system_card(tmp_path):
    """The Knowledge facade exposes `card` as a property; the light path
    must read it, not call it (a TypeError on a real installation)."""
    from curie.memory import Knowledge, KnowledgeStore
    from tests.test_knowledge import setup as setup_with_knowledge

    root = tmp_path / "kb"
    root.mkdir()
    (root / "CARD.md").write_text("System card: leasing agreements in ZORDERS.", encoding="utf-8")
    knowledge = Knowledge(KnowledgeStore(root, refresh_every=0), tmp_path / "kb" / ".build")

    async def scenario():
        llm = RecordingLLM([("Użyj FD10.", None)])
        mgr, orch, flow, session, q = setup_with_knowledge(tmp_path, llm, knowledge)
        orch.start_turn(session, "Pokaż mi jak wyświetlić saldo klienta w FI za pomocą SAP GUI")
        await session.turn_task
        assert session.thread_light and llm.kwargs[0]["tools"] is None
        assert "System card: leasing agreements in ZORDERS." in llm.seen[0][0]["content"]
        assert not any(e == "notice" and "turn failed" in str(d) for e, d in drain(q))

    asyncio.run(scenario())


def test_user_message_is_the_first_event_of_a_turn(tmp_path):
    """Nothing that takes time (catalogue, router, model) runs before the
    user's message is in the browser."""
    async def scenario():
        llm = RecordingLLM([("Odpowiedź.", None)])
        mgr, orch, flow, session, q = setup(tmp_path, llm)
        orch.start_turn(session, "Ile umów zawarto w 2004 roku w ZUMOWYL?")
        await session.turn_task
        kinds = [e for e, d in drain(q) if e not in ("state",)]
        assert kinds[0] == "user_message" and kinds[1] == "assistant_start"
        assert session.thread == ["Ile umów zawarto w 2004 roku w ZUMOWYL?"] and session.thread_id

    asyncio.run(scenario())
