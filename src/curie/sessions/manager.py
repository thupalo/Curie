"""SessionManager: the one owner of all sessions.

Two kinds of callers:

* RFC handler threads (Z_AI_SESSION_OPEN, Z_AI_HEARTBEAT, ...) -- they must
  return quickly, so the sync methods here only take a lock, update fields and
  hand anything that touches asyncio to the loop with call_soon_threadsafe.
* The asyncio side (web endpoints, the orchestrator) -- coroutines and plain
  methods called on the loop thread.

emit() works from both: it appends to the transcript, writes the JSONL log,
and pushes to every SSE subscriber queue on the loop thread.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from curie import config
from curie.sessions.model import HEARTBEAT_STATE, AbapRun, SapIdentity, Session, State

log = logging.getLogger(__name__)

# Events kept in the transcript for replay. Streaming deltas are not: the
# final assistant_done carries the whole text.
PERSISTED_EVENTS = frozenset({
    "user_message", "assistant_start", "assistant_done", "tool_call",
    "tool_result", "notice", "code_ready", "run_started", "run_result",
    "syntax_error",
})

_current: "SessionManager | None" = None


def current() -> "SessionManager":
    """The process-wide manager, for RFC handlers that cannot be passed one."""
    if _current is None:
        raise RuntimeError("SessionManager not initialised")
    return _current


class SessionLimit(RuntimeError):
    """SESSION_MAX open sessions already."""


class SessionManager:
    def __init__(self, public_url: str | None = None, log_dir: str | None = None):
        global _current
        self.public_url = (public_url or config.WEB_PUBLIC_URL).rstrip("/")
        self.log_dir = Path(log_dir or config.SESSION_LOG_DIR)
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._monitor_task: asyncio.Task | None = None
        self._log_lock = threading.Lock()
        # Set by cli.py: coroutine (session, reason) run when a session is
        # closed, e.g. to abort a job. Optional.
        self.on_close = None
        self._close_tasks: set = set()       # running on_close callbacks; stop() waits for them
        _current = self

    # -- loop binding ------------------------------------------------------- #

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._loop_thread = threading.get_ident()

    def _on_loop(self, fn, *args) -> None:
        """Run fn(*args) on the loop thread, now or soon."""
        if self._loop is None or self._loop.is_closed():
            return
        if threading.get_ident() == self._loop_thread:
            fn(*args)
        else:
            self._loop.call_soon_threadsafe(fn, *args)

    # -- lookup --------------------------------------------------------------- #

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def open_sessions(self) -> list[Session]:
        with self._lock:
            return [s for s in self._sessions.values() if not s.closed]

    def url_for(self, session: Session) -> str:
        return f"{self.public_url}/s/{session.id}"

    # -- RFC side (sync, any thread) ----------------------------------------- #

    def open_from_rfc(self, attrs: dict, prompt: str = "") -> Session:
        identity = SapIdentity.from_attributes(attrs)
        with self._lock:
            open_sessions = [s for s in self._sessions.values() if not s.closed]
            if len(open_sessions) >= config.SESSION_MAX:
                raise SessionLimit(f"{len(open_sessions)} sessions open")
            same_user = [s for s in open_sessions
                         if (s.sap.sysid, s.sap.client, s.sap.user)
                         == (identity.sysid, identity.client, identity.user)]
            if len(same_user) >= config.SESSION_MAX_PER_USER:
                raise SessionLimit(f"user {identity.user} already has "
                                   f"{len(same_user)} open session(s)")
            # Upper-case hex on purpose: the id travels through ABAP CHAR32
            # fields and PARAMETERS without LOWER CASE, which upper-case it.
            session = Session(
                id=secrets.token_hex(16).upper(),
                sap=identity,
                initial_prompt=(prompt or "").strip(),
            )
            self._sessions[session.id] = session
        self._log(session, "session_open", {
            "sap": session.sap.as_dict(), "prompt": session.initial_prompt,
            "url": self.url_for(session)})
        log.info("session %s opened by %s/%s %s", session.id[:8],
                 session.sap.sysid, session.sap.client, session.sap.user)
        return session

    def open_dev(self, prompt: str = "", user: str = "") -> Session:
        """A session without SAP GUI (CURIE_DEV); identity from the logon."""
        session = self.open_from_rfc({
            "sysId": "DEV", "client": config.SAP_RFC["client"],
            "user": user or config.SAP_RFC["user"], "partnerRel": config.SAP_RELEASE,
        }, prompt)
        session.dev = True
        return session

    def heartbeat(self, session_id: str) -> tuple[str, str]:
        """Record a SAP heartbeat; returns (E_STATE, E_STATUS_TEXT)."""
        session = self.get(session_id)
        if session is None:
            raise KeyError(session_id)
        with session.lock:
            session.sap_last_seen = time.time()
            return HEARTBEAT_STATE[session.state], session.state_text[:80]

    def close_from_rfc(self, session_id: str, reason: str = "sap_closed") -> None:
        session = self.get(session_id)
        if session is not None:
            self.close(session, reason)

    def deliver_list(self, session_id: str, run_id: str, rc: int,
                     message: str, lines: list[str]) -> bool:
        """Result of a background run arrived from ZAI_RUNNER."""
        session = self.get(session_id)
        if session is None:
            return False
        run = session.runs.get(run_id)
        if run is None:
            return False
        result = {"ok": rc == 0, "rc": rc, "message": message, "lines": lines}
        self._log(session, "list_delivered", {"run_id": run_id, "rc": rc,
                                              "lines": len(lines)})

        def resolve():
            fut = run.delivery
            if fut is not None and not fut.done():
                fut.set_result(result)
            else:
                # Late delivery (timeout already reported): show it anyway.
                run.result = result
                run.status = "done" if result["ok"] else "failed"
                self.emit(session, "run_result", {"run_id": run_id, **result})

        self._on_loop(resolve)
        return True

    # -- state and events (any thread) ---------------------------------------- #

    def set_state(self, session: Session, state: State, text: str = "") -> None:
        with session.lock:
            if session.closed:
                return
            session.state = state
            session.state_text = text
        self.emit(session, "state", {"state": state.value, "text": text},
                  persist=False)

    def emit(self, session: Session, event: str, data: dict,
             persist: bool | None = None) -> None:
        if persist is None:
            persist = event in PERSISTED_EVENTS
        if persist:
            with session.lock:
                session.transcript.append((event, data))
                session.last_activity = time.time()
            self._log(session, event, data)

        def push():
            for q in list(session.subscribers):
                try:
                    q.put_nowait((event, data))
                except asyncio.QueueFull:
                    pass

        self._on_loop(push)

    def notice(self, session: Session, text: str, level: str = "info") -> None:
        self.emit(session, "notice", {"level": level, "text": text})

    def close(self, session: Session, reason: str) -> None:
        with session.lock:
            if session.closed:
                return
            session.state = State.CLOSED
            session.state_text = "closed"
            session.close_reason = reason
        log.info("session %s closed: %s", session.id[:8], reason)
        self._log(session, "session_closed", {"reason": reason})

        def finish():
            task = session.turn_task
            if task is not None and not task.done():
                task.cancel()
            for run in session.runs.values():
                fut = run.delivery
                if fut is not None and not fut.done():
                    fut.cancel()
            for q in list(session.subscribers):
                try:
                    q.put_nowait(("session_closed", {"reason": reason}))
                except asyncio.QueueFull:
                    pass
            if self.on_close is not None:
                task = asyncio.ensure_future(self.on_close(session, reason))
                self._close_tasks.add(task)
                task.add_done_callback(self._close_tasks.discard)

        self._on_loop(finish)

    # -- browser side (loop thread) ------------------------------------------- #

    def bind_client(self, session: Session, ip: str | None) -> bool:
        """Accept a browser request from `ip`; False when the session belongs
        to another address (SESSION_BIND_IP)."""
        if not config.SESSION_BIND_IP or not ip:
            return True
        with session.lock:
            if session.client_ip is None:
                session.client_ip = ip
                bound = True
            else:
                bound = session.client_ip == ip
        if bound and session.client_ip == ip and session.browser_last_seen is None:
            self._log(session, "browser_bound", {"ip": ip})
        elif not bound:
            log.warning("session %s: request from %s, bound to %s",
                        session.id[:8], ip, session.client_ip)
        return bound

    def touch_browser(self, session: Session) -> None:
        with session.lock:
            first = session.browser_last_seen is None
            session.browser_last_seen = time.time()
            if first and session.state is State.CREATED:
                session.state = State.IDLE
                session.state_text = "Browser connected"
        if first:
            self.emit(session, "state", {"state": session.state.value,
                                          "text": session.state_text},
                      persist=False)
            self._log(session, "browser_attached", {})

    def subscribe(self, session: Session) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10000)
        q.put_nowait(("hello", session.hello()))
        with session.lock:
            replay = list(session.transcript)
        for event, data in replay:
            q.put_nowait((event, data))
        q.put_nowait(("state", {"state": session.state.value,
                                "text": session.state_text}))
        if session.closed:
            q.put_nowait(("session_closed", {"reason": session.close_reason}))
        session.subscribers.append(q)
        return q

    def unsubscribe(self, session: Session, q: asyncio.Queue) -> None:
        if q in session.subscribers:
            session.subscribers.remove(q)

    def reset(self, session: Session) -> None:
        """/reset: forget the conversation, keep the session (loop thread).

        A running turn is cancelled first; a running SAP job keeps running
        but its result is no longer waited for (the caller aborts it).
        """
        task = session.turn_task
        if task is not None and not task.done():
            task.cancel()
        for run in session.runs.values():
            fut = run.delivery
            if fut is not None and not fut.done():
                fut.cancel()
            if run.status in ("ready", "blocked", "checking"):
                run.status = "discarded"
        with session.lock:
            session.messages.clear()
            session.transcript.clear()
            session.last_activity = time.time()
        self._log(session, "session_reset", {})
        self.emit(session, "session_reset", {"text": "Conversation cleared. The session and "
                                                     "the SAP connection stay open."},
                  persist=False)
        self.set_state(session, State.IDLE, "")

    def new_run(self, session: Session, progname: str, source: str) -> AbapRun:
        run = AbapRun(run_id=secrets.token_hex(4).upper(), progname=progname,
                      source=source)
        session.runs[run.run_id] = run
        return run

    # -- monitor ---------------------------------------------------------------- #

    def start_monitor(self) -> None:
        self._monitor_task = asyncio.ensure_future(self._monitor())

    async def stop(self, reason: str = "shutdown", wait: float = 15.0) -> None:
        """Close every open session and wait for the close callbacks (the
        archive import of each session's log) to finish, up to `wait`
        seconds. Safe to call twice."""
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None
        for session in self.open_sessions():
            self.close(session, reason)
        await asyncio.sleep(0)  # let the close callbacks start
        pending = [t for t in self._close_tasks if not t.done()]
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), wait)
            except asyncio.TimeoutError:
                log.warning("%d session close callback(s) still running after %.0fs", len(pending), wait)

    async def _monitor(self) -> None:
        while True:
            try:
                await asyncio.sleep(1.0)
                self._sweep()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("session monitor")

    def _sweep(self) -> None:
        now = time.time()
        for session in self.open_sessions():
            with session.lock:
                sap_age = now - session.sap_last_seen
                attached = session.browser_last_seen is not None
                browser_age = now - (session.browser_last_seen or session.created)
                state = session.state
                idle_age = now - session.last_activity
            if sap_age > config.SESSION_HEARTBEAT_TIMEOUT and not session.dev:
                self.close(session, "sap_gone")
            elif not attached and browser_age > config.BROWSER_ATTACH_TIMEOUT:
                self.close(session, "attach_timeout")
            elif attached and browser_age > config.BROWSER_TIMEOUT \
                    and not session.subscribers:
                self.close(session, "browser_gone")
            elif state in (State.IDLE, State.CONFIRM) \
                    and idle_age > config.SESSION_IDLE_TIMEOUT:
                self.close(session, "idle")

    # -- log -------------------------------------------------------------------- #

    def _log(self, session: Session, kind: str, data: dict) -> None:
        try:
            day = datetime.now().strftime("%Y-%m-%d")
            path = self.log_dir / day / f"{session.id}.jsonl"
            record = {"ts": time.time(), "kind": kind, **_jsonable(data)}
            with self._log_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("session log: %s", exc)

    def log_llm(self, session: Session, kind: str, data: dict) -> None:
        """Extra audit-trail records (prompts, model messages, verdicts)."""
        self._log(session, kind, data)


def _jsonable(data: dict) -> dict:
    out = {}
    for key, value in data.items():
        try:
            json.dumps(value)
            out[key] = value
        except (TypeError, ValueError):
            out[key] = repr(value)
    return out
