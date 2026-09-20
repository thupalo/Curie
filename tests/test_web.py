"""The HTTP layer with the fakes from test_orchestrator.

The TestClient hands a streaming body over only once the response has ended,
so the SSE tests close the session first: the event stream then replays the
transcript, ends with session_closed and returns.
"""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from curie import config
from curie.llm.abap_flow import AbapFlow
from curie.llm.orchestrator import Orchestrator
from curie.sessions.manager import SessionManager
from tests.test_orchestrator import ATTRS, FakeExecutor, FakeLLM, FakeToolBox
from curie.web.app import create_app


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)
    mgr = SessionManager(public_url="http://c", log_dir=str(tmp_path))
    llm = FakeLLM([("Answer one.", None), ("Answer two.", None)])
    orch = Orchestrator(mgr, llm, FakeToolBox())
    flow = AbapFlow(mgr, FakeExecutor(), llm, orch)
    orch.flow = flow
    app = create_app(mgr, orch, flow)

    @app.on_event("startup")
    async def _bind():
        mgr.bind_loop(asyncio.get_running_loop())

    with TestClient(app) as client:
        yield mgr, client


def sse_events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event:
            out.append((event, data))
    return out


def wait_turn(session, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = session.turn_task
        if task is not None and task.done():
            return
        time.sleep(0.02)
    raise AssertionError("turn did not finish")


def test_unknown_token_is_404_and_page_serves(stack):
    mgr, client = stack
    assert client.get("/s/nope").status_code == 404
    assert client.get("/healthz").json()["ok"] is True
    session = mgr.open_from_rfc(ATTRS, "hello")
    r = client.get(f"/s/{session.id}")
    assert r.status_code == 200 and "app.js" in r.text
    assert client.get("/static/app.js").status_code == 200


def test_prompt_lifecycle_and_replay(stack):
    mgr, client = stack
    session = mgr.open_from_rfc(ATTRS, "initial")
    sid = session.id

    assert client.post(f"/s/{sid}/ping").json()["state"] == "IDLE"
    assert client.post(f"/s/{sid}/prompt", json={"text": ""}).status_code == 400
    assert client.post(f"/s/{sid}/prompt", json={"text": "hi"}).status_code == 202
    wait_turn(session)
    assert session.messages[-1] == {"role": "assistant", "content": "Answer one."}

    assert client.post(f"/s/{sid}/runs/XXXX/confirm").status_code == 409
    assert client.post(f"/s/{sid}/close").json()["ok"] is True
    assert session.closed and session.close_reason == "browser_gone"
    assert client.post(f"/s/{sid}/ping").status_code == 410
    assert client.post(f"/s/{sid}/prompt", json={"text": "x"}).status_code == 410

    r = client.get(f"/s/{sid}/events")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r.text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "hello" and kinds[-1] == "session_closed"
    assert "user_message" in kinds and "assistant_done" in kinds
    assert events[0][1]["initial_prompt"] == "initial"
    assert events[0][1]["sap"]["user"] == "HUPALO"
    assert events[-1][1] == {"reason": "browser_gone"}
    assert [d["text"] for e, d in events if e == "assistant_done"] == ["Answer one."]


def test_busy_turn_is_409(stack):
    mgr, client = stack
    session = mgr.open_from_rfc(ATTRS, "")
    client.post(f"/s/{session.id}/ping")
    assert client.post(f"/s/{session.id}/prompt", json={"text": "one"}).status_code == 202
    # The second request lands while the first turn task is still pending.
    second = client.post(f"/s/{session.id}/prompt", json={"text": "two"}).status_code
    assert second in (202, 409)
    wait_turn(session)
    assert client.post(f"/s/{session.id}/stop").json()["stopped"] is False


def test_ip_binding_rejects_other_client(stack, monkeypatch):
    monkeypatch.setattr(config, "SESSION_BIND_IP", True)
    mgr, client = stack
    session = mgr.open_from_rfc(ATTRS, "")
    # Bound to another PC first: the test client's requests are refused.
    assert mgr.bind_client(session, "10.9.9.9") is True
    assert client.post(f"/s/{session.id}/ping").status_code == 403
    assert client.get(f"/s/{session.id}").status_code == 403
    assert client.post(f"/s/{session.id}/close").json()["ok"] is True   # ignored, not closed
    assert not session.closed
    # A fresh session binds to whoever comes first.
    fresh = mgr.open_from_rfc(dict(ATTRS, user="B"), "")
    assert client.post(f"/s/{fresh.id}/ping").status_code == 200
    assert fresh.client_ip == "testclient"


def test_reset_command_clears_conversation(stack):
    mgr, client = stack
    session = mgr.open_from_rfc(ATTRS, "")
    sid = session.id
    client.post(f"/s/{sid}/ping")
    assert client.post(f"/s/{sid}/prompt", json={"text": "hi"}).status_code == 202
    wait_turn(session)
    assert session.messages and session.transcript
    r = client.post(f"/s/{sid}/prompt", json={"text": " /Reset "})
    assert r.status_code == 200 and r.json()["command"] == "reset"
    assert session.messages == [] and session.transcript == []
    assert session.state.value == "IDLE" and not session.closed
    # the next prompt starts a fresh conversation in the same session
    assert client.post(f"/s/{sid}/prompt", json={"text": "again"}).status_code == 202
    wait_turn(session)
    assert [m["content"] for m in session.messages] == ["again", "Answer two."]
    assert client.post(f"/s/{sid}/reset").json()["ok"] is True
    assert session.messages == []


def test_healthz_caches_and_flush(stack):
    mgr, client = stack
    h = client.get("/healthz").json()
    assert "caches" in h and isinstance(h["caches"], dict)
    r = client.post("/admin/cache/flush")     # the test client counts as local
    assert r.status_code == 200 and "flushed" in r.json()
