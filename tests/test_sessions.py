import asyncio
import time

from curie import config
from curie.handlers.session import (z_ai_heartbeat, z_ai_list_deliver, z_ai_session_close,
                              z_ai_session_open)
from curie.sapnwrfc720 import ABAPException
from curie.sessions.manager import SessionManager
from curie.sessions.model import State

ATTRS = {"sysId": "R45", "client": "100", "user": "HUPALO", "partnerRel": "45B"}


def ctx():
    return {"function_name": "X", "connection_attributes": ATTRS}


def test_open_heartbeat_close_via_handlers(tmp_path):
    mgr = SessionManager(public_url="http://curie:8765", log_dir=str(tmp_path))
    out = z_ai_session_open(ctx(), I_PROMPT="how many plants?")
    sid = out["E_SESSION_ID"]
    assert len(sid) == 32 and out["E_URL"] == f"http://curie:8765/s/{sid}"
    assert out["E_HEARTBEAT_SEC"] == config.SESSION_HEARTBEAT_SEC
    session = mgr.get(sid)
    assert session.sap.user == "HUPALO" and session.initial_prompt == "how many plants?"
    assert session.state is State.CREATED

    hb = z_ai_heartbeat(ctx(), I_SESSION_ID=sid)
    assert hb == {"E_STATE": "WAITING", "E_STATUS_TEXT": ""}
    try:
        z_ai_heartbeat(ctx(), I_SESSION_ID="nope")
        assert False, "expected UNKNOWN_SESSION"
    except ABAPException as exc:
        assert exc.key == "UNKNOWN_SESSION"

    z_ai_session_close(ctx(), I_SESSION_ID=sid, I_REASON="user_left")
    assert session.closed and session.close_reason == "user_left"
    assert z_ai_heartbeat(ctx(), I_SESSION_ID=sid)["E_STATE"] == "CLOSED"
    log_files = list(tmp_path.rglob("*.jsonl"))
    assert len(log_files) == 1 and "session_closed" in log_files[0].read_text(encoding="utf-8")


def test_long_prompt_from_table(tmp_path):
    SessionManager(public_url="http://c", log_dir=str(tmp_path))
    out = z_ai_session_open(ctx(), I_PROMPT="", T_PROMPT=[{"LINE": "line one   "}, {"LINE": "line two"}])
    from curie.sessions.manager import current
    assert current().get(out["E_SESSION_ID"]).initial_prompt == "line one\nline two"


def test_subscribe_replays_transcript_and_state(tmp_path):
    async def scenario():
        mgr = SessionManager(public_url="http://c", log_dir=str(tmp_path))
        mgr.bind_loop(asyncio.get_running_loop())
        session = mgr.open_from_rfc(ATTRS, "p")
        mgr.touch_browser(session)
        assert session.state is State.IDLE
        mgr.emit(session, "user_message", {"id": "1", "text": "hi"})
        mgr.emit(session, "assistant_delta", {"id": "2", "text": "partial"})  # not persisted
        q = mgr.subscribe(session)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        kinds = [e for e, _ in events]
        assert kinds == ["hello", "user_message", "state"]
        assert events[0][1]["initial_prompt"] == "p"
        # live event reaches the subscriber, from a foreign thread too
        await asyncio.get_running_loop().run_in_executor(
            None, mgr.emit, session, "notice", {"level": "info", "text": "x"})
        await asyncio.sleep(0.05)
        assert q.get_nowait() == ("notice", {"level": "info", "text": "x"})
        mgr.close(session, "browser_gone")
        await asyncio.sleep(0)
        assert q.get_nowait() == ("session_closed", {"reason": "browser_gone"})

    asyncio.run(scenario())


def test_monitor_closes_on_missing_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_HEARTBEAT_TIMEOUT", 0.05)

    async def scenario():
        mgr = SessionManager(public_url="http://c", log_dir=str(tmp_path))
        mgr.bind_loop(asyncio.get_running_loop())
        session = mgr.open_from_rfc(ATTRS, "")
        cancelled = asyncio.Event()

        async def fake_turn():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        session.turn_task = asyncio.create_task(fake_turn())
        await asyncio.sleep(0)  # let the task start, so cancellation reaches it
        session.sap_last_seen = time.time() - 1
        mgr._sweep()
        await asyncio.sleep(0.01)
        assert session.closed and session.close_reason == "sap_gone"
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_list_delivery_resolves_future(tmp_path):
    async def scenario():
        mgr = SessionManager(public_url="http://c", log_dir=str(tmp_path))
        mgr.bind_loop(asyncio.get_running_loop())
        session = mgr.open_from_rfc(ATTRS, "")
        run = mgr.new_run(session, "ZAI_1", "REPORT zai_1.")
        run.delivery = asyncio.get_running_loop().create_future()
        out = z_ai_list_deliver(ctx(), I_SESSION_ID=session.id, I_RUN_ID=run.run_id,
                                I_RC=0, T_LIST=[{"LINE": "Hello "}, {"LINE": "World"}])
        assert out == {"T_LIST": []}
        result = await asyncio.wait_for(run.delivery, 1)
        assert result["ok"] and result["lines"] == ["Hello", "World"]
        try:
            z_ai_list_deliver(ctx(), I_SESSION_ID=session.id, I_RUN_ID="XXXX", I_RC=0, T_LIST=[])
            assert False
        except ABAPException as exc:
            assert exc.key == "UNKNOWN_RUN"

    asyncio.run(scenario())


def test_per_user_limit_and_ip_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_MAX_PER_USER", 2)
    monkeypatch.setattr(config, "SESSION_BIND_IP", True)
    mgr = SessionManager(public_url="http://c", log_dir=str(tmp_path))
    a = mgr.open_from_rfc(ATTRS, "")
    mgr.open_from_rfc(ATTRS, "")
    try:
        mgr.open_from_rfc(ATTRS, "")
        assert False, "third session for the same user must be refused"
    except Exception as exc:
        assert "already has" in str(exc)
    other = dict(ATTRS, user="OTHER")
    mgr.open_from_rfc(other, "")  # a different user is fine
    assert mgr.bind_client(a, "10.0.0.1") is True
    assert mgr.bind_client(a, "10.0.0.1") is True
    assert mgr.bind_client(a, "10.0.0.2") is False
    monkeypatch.setattr(config, "SESSION_BIND_IP", False)
    assert mgr.bind_client(a, "10.0.0.2") is True


def test_stop_waits_for_the_close_callbacks():
    """The archive import runs as a close callback; shutdown must wait for
    it instead of letting the process end with the import half done."""
    from curie.sessions.manager import SessionManager
    from tests.test_orchestrator import ATTRS

    async def scenario():
        mgr = SessionManager(public_url="http://c")
        mgr.bind_loop(asyncio.get_running_loop())
        done = []

        async def slow_close(session, reason):
            await asyncio.sleep(0.05)
            done.append((session.id, reason))

        mgr.on_close = slow_close
        s1 = mgr.open_from_rfc(ATTRS, "")
        s2 = mgr.open_from_rfc({**ATTRS, "user": "OTHER"}, "")
        await mgr.stop("shutdown")
        assert sorted(r for _, r in done) == ["shutdown", "shutdown"] and {i for i, _ in done} == {s1.id, s2.id}
        await mgr.stop("shutdown")          # idempotent

    asyncio.run(scenario())


def test_web_server_closes_sessions_on_the_shutdown_signal():
    import signal
    from curie.cli import build_web_server
    from curie.sessions.manager import SessionManager
    from tests.test_orchestrator import ATTRS

    async def scenario():
        loop = asyncio.get_running_loop()
        mgr = SessionManager(public_url="http://c")
        mgr.bind_loop(loop)
        session = mgr.open_from_rfc(ATTRS, "")
        server = build_web_server(object(), mgr, loop)
        server.handle_exit(signal.SIGINT, None)
        assert server.should_exit
        await asyncio.sleep(0.05)
        assert session.closed and session.close_reason == "shutdown"

    asyncio.run(scenario())
