"""From a session log (JSONL) to episodes: the unit of a recording.

An episode is one thread of one session: the business question, the
follow-ups that refined it, every model call and tool call on the way,
every program version with its verdicts, the run, the outcome, the
feedback, and what the user did next. The session logs are the raw
stream; this module is the deterministic pipeline P1 of the knowledge
plan. It copes with the log format before 11 September 2026 (no thread
ids, no llm_call records, tool results as summaries only) as far as the
records allow.

    session, episodes = build_session(path)
    episode.question, .refinements, .outcome, .calls, .tool_calls,
    .programs, .feedback, .next_action, .next_gap

Outcomes:
    confirmed_ran_ok   a program was confirmed, ran and delivered a list
    ran_failed         a program ran and failed (runtime, aborted, no list)
    discarded          the user discarded the program without running it
    abandoned_code     runnable code was shown but never confirmed
    no_program         syntax or rules rounds ended without a program
    denied             the audit refused every program of the thread
    interrupted        the user stopped the model
    answered           the model answered without a program
    command            a slash command
    empty              nothing happened after the question
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from curie.memory.threads import is_followup

log = logging.getLogger(__name__)

COMMAND = re.compile(r"^\s*/[a-z]+", re.IGNORECASE)


@dataclass
class SessionRecord:
    id: str
    log_path: str
    sysid: str = ""
    client: str = ""
    user: str = ""
    language: str = ""
    release: str = ""
    opened: float = 0.0
    closed: float = 0.0
    reason: str = ""
    initial_prompt: str = ""
    selftest: bool = False


@dataclass
class Episode:
    id: str
    session_id: str
    seq: int
    thread: str = ""
    question: str = ""
    refinements: list = field(default_factory=list)
    language: str = ""
    started: float = 0.0
    ended: float = 0.0
    outcome: str = "empty"
    calls: list = field(default_factory=list)        # model calls
    tool_calls: list = field(default_factory=list)
    programs: list = field(default_factory=list)     # by run_id, in order
    feedback: list = field(default_factory=list)     # {"target", "id", "value", "ts"}
    notices: list = field(default_factory=list)      # error/warn notices
    next_action: str = ""     # followup | question | reset | close | none
    next_gap: float = 0.0
    exemplar_id: str = ""
    origin: dict | None = None
    events: list = field(default_factory=list)       # the raw records of the episode

    @property
    def tokens_in(self) -> int:
        return sum(int(c.get("tokens_in") or 0) for c in self.calls)

    @property
    def tokens_out(self) -> int:
        return sum(int(c.get("tokens_out") or 0) for c in self.calls)

    @property
    def syntax_rounds(self) -> int:
        return sum(1 for p in self.programs if p.get("syntax") and not p["syntax"].get("ok", True))

    def summary(self) -> dict:
        return {"id": self.id, "session": self.session_id, "seq": self.seq, "question": self.question,
                "refinements": len(self.refinements), "outcome": self.outcome,
                "calls": len(self.calls), "tool_calls": len(self.tool_calls),
                "programs": len(self.programs), "feedback": len(self.feedback)}


def detect_language(text: str) -> str:
    """The language of a question among the ones curie/lexicon.py knows
    (upper-cased ISO code), "" when nothing identifies it."""
    from curie import lexicon

    return lexicon.detect(text)


def read_records(paths) -> list[dict]:
    out = []
    if isinstance(paths, (str, Path)):
        paths = [paths]
    for path in paths:
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and "kind" in rec:
                    out.append(rec)
    out.sort(key=lambda r: float(r.get("ts") or 0))
    return out


def session_files(path: Path) -> list[Path]:
    """All log files of the session `path` belongs to: a session that runs
    past midnight continues in the next day's folder under the same id."""
    path = Path(path)
    root = path.parent.parent
    siblings = sorted(p for p in root.glob(f"*/{path.name}") if p.is_file()) if root.is_dir() else []
    return siblings or [path]


def build_session(path: Path) -> tuple[SessionRecord, list[Episode]]:
    path = Path(path)
    files = session_files(path)
    records = read_records(files)
    session = SessionRecord(id=path.stem, log_path=";".join(str(p) for p in files))
    for rec in records:
        if rec["kind"] == "session_open":
            sap = rec.get("sap") or {}
            session.sysid = str(sap.get("sysid", "")).strip()
            session.client = str(sap.get("client", "")).strip()
            session.user = str(sap.get("user", "")).strip()
            session.language = str(sap.get("language", "")).strip()
            session.release = str(sap.get("release", "")).strip()
            session.opened = float(rec.get("ts") or 0)
            session.initial_prompt = str(rec.get("prompt") or "")
            session.selftest = session.initial_prompt.lower() == "selftest"
        elif rec["kind"] == "session_closed":
            session.closed = float(rec.get("ts") or 0)
            session.reason = str(rec.get("reason") or "")
    return session, _episodes(session, records)


# --------------------------------------------------------------------------- #
# Splitting into episodes
# --------------------------------------------------------------------------- #


def _episodes(session: SessionRecord, records: list[dict]) -> list[Episode]:
    episodes: list[Episode] = []
    current: Episode | None = None
    seen_ids: set[str] = set()

    def new_episode(rec: dict, text: str) -> Episode:
        uid = str(rec.get("id") or f"{int(float(rec.get('ts') or 0))}")
        eid = f"{session.id[:8]}-{len(episodes) + 1:03d}"
        ep = Episode(id=eid, session_id=session.id, seq=len(episodes) + 1,
                     thread=str(rec.get("thread") or uid), question=text,
                     language=detect_language(text), started=float(rec.get("ts") or 0))
        episodes.append(ep)
        return ep

    for rec in records:
        kind = rec["kind"]
        if kind == "user_message":
            text = str(rec.get("text") or "").strip()
            if "thread" in rec:                      # logs from 11 September on
                followup = bool(rec.get("followup")) and current is not None
            else:
                followup = current is not None and current.outcome != "command" \
                    and is_followup(text, has_thread=True) and not COMMAND.match(text)
            if followup and current is not None:
                current.refinements.append(text)
                current.events.append(rec)
                continue
            if current is not None:
                _close(current, rec, "question")
            current = new_episode(rec, text)
            if COMMAND.match(text):
                current.outcome = "command"
            current.events.append(rec)
            continue
        if kind == "session_reset":
            if current is not None:
                _close(current, rec, "reset")
                current = None
            continue
        if kind == "session_closed":
            if current is not None:
                _close(current, rec, "close")
                current = None
            continue
        if current is None:
            continue
        current.events.append(rec)
        _absorb(current, rec, seen_ids)
    for ep in episodes:
        if not ep.ended:
            ep.ended = float(ep.events[-1].get("ts") or ep.started) if ep.events else ep.started
        _finish(ep)
    return episodes


def _close(ep: Episode, rec: dict, next_action: str) -> None:
    ts = float(rec.get("ts") or 0)
    ep.ended = float(ep.events[-1].get("ts") or ep.started) if ep.events else ep.started
    ep.next_action = next_action
    ep.next_gap = max(0.0, ts - ep.ended)


def _absorb(ep: Episode, rec: dict, seen_ids: set[str]) -> None:
    kind = rec["kind"]
    ts = float(rec.get("ts") or 0)
    if kind == "llm_call":
        # assistant_start (emitted first) already opened the call; fill it.
        call = _call(ep, rec.get("id"))
        if call is None:
            call = {"id": rec.get("id"), "ts": ts, "finish": "", "tool_calls": 0, "chars": 0,
                    "ms": 0, "tokens_in": 0, "tokens_out": 0, "text": ""}
            ep.calls.append(call)
        call.update({"model": rec.get("model", ""), "temperature": rec.get("temperature"),
                     "max_tokens": rec.get("max_tokens"), "tool_mode": rec.get("tool_mode", ""),
                     "tools": rec.get("tools", 0), "notes": list(rec.get("notes") or []),
                     "exemplars": list(rec.get("exemplars") or []),
                     "knowledge_chars": rec.get("knowledge_chars", 0),
                     "messages": rec.get("messages", 0), "history_chars": rec.get("history_chars", 0)})
    elif kind == "assistant_start":
        cid = rec.get("id")
        if not any(c.get("id") == cid for c in ep.calls):
            ep.calls.append({"id": cid, "ts": ts, "model": "", "temperature": None,
                             "max_tokens": None, "tool_mode": "", "tools": 0, "notes": [],
                             "exemplars": [], "knowledge_chars": 0, "messages": 0,
                             "history_chars": 0, "finish": "", "tool_calls": 0, "chars": 0,
                             "ms": 0, "tokens_in": 0, "tokens_out": 0, "text": ""})
    elif kind == "llm_done":
        call = _call(ep, rec.get("id"))
        if call is not None:
            call.update({"finish": rec.get("finish") or "", "tool_calls": rec.get("tool_calls", 0),
                         "chars": rec.get("chars", 0), "ms": rec.get("ms", 0),
                         "tokens_in": rec.get("tokens_in", 0), "tokens_out": rec.get("tokens_out", 0)})
    elif kind == "assistant_done":
        call = _call(ep, rec.get("id"))
        if call is not None:
            call["text"] = str(rec.get("text") or "")
            if not call["ms"]:
                call["ms"] = int((ts - call["ts"]) * 1000) if call["ts"] else 0
            if rec.get("interrupted"):
                call["finish"] = "interrupted"
            elif rec.get("truncated"):
                call["finish"] = "length"
    elif kind == "tool":
        tid = str(rec.get("id") or "")
        if not tid:
            # Old logs: the `tool` record repeats the tool_call/tool_result
            # pair that precedes it; attach its result to that pair instead.
            dup = "dup:" + str(rec.get("name")) + json.dumps(rec.get("args") or {}, sort_keys=True)
            if dup in seen_ids:
                seen_ids.discard(dup)
                tc = next((t for t in reversed(ep.tool_calls)
                           if t["name"] == rec.get("name") and t["args"] == (rec.get("args") or {})), None)
                if tc is not None and tc.get("result") is None:
                    tc["result"] = rec.get("result")
                return
            tid = f"t{len(ep.tool_calls) + 1}"
        if tid in seen_ids:
            # New logs: the tool_call/tool_result pair opened the entry; the
            # `tool` record adds the result and the turn it belongs to.
            tc = next((t for t in ep.tool_calls if t["id"] == tid), None)
            if tc is not None:
                tc.update({"call_id": rec.get("turn", tc.get("call_id", "")),
                           "ok": bool(rec.get("ok")), "summary": rec.get("summary", tc.get("summary")),
                           "ms": rec.get("ms", tc.get("ms")), "result": rec.get("result")})
            return
        seen_ids.add(tid)
        ep.tool_calls.append({"id": tid, "call_id": rec.get("turn", ""), "ts": ts,
                              "name": rec.get("name", ""), "args": rec.get("args") or {},
                              "ok": bool(rec.get("ok")), "summary": rec.get("summary", ""),
                              "ms": rec.get("ms", 0), "result": rec.get("result")})
    elif kind == "tool_call":
        # Old logs: tool_call/tool_result pairs and a `tool` record; the
        # pair carries the id, the `tool` record duplicates it without one.
        tid = str(rec.get("id") or "")
        if tid and tid not in seen_ids:
            seen_ids.add(tid)
            ep.tool_calls.append({"id": tid, "call_id": "", "ts": ts, "name": rec.get("name", ""),
                                  "args": rec.get("args") or {}, "ok": None, "summary": "",
                                  "ms": 0, "result": None})
    elif kind == "tool_result":
        tc = next((t for t in ep.tool_calls if t["id"] == str(rec.get("id"))), None)
        if tc is not None:
            tc.update({"ok": bool(rec.get("ok")), "summary": rec.get("summary", ""),
                       "ms": rec.get("ms", 0)})
            seen_ids.add("dup:" + str(tc["name"]) + json.dumps(tc["args"], sort_keys=True))
    elif kind in ("code_checked", "code_ready", "audit", "code_confirmed", "run_started",
                  "run_result", "syntax_error", "code_discarded", "run_aborted", "list_delivered",
                  "exemplar_saved", "catalogue_replay"):
        _program_event(ep, kind, rec, ts)
    elif kind == "feedback":
        ep.feedback.append({"target": rec.get("target"), "id": rec.get("id"),
                            "value": rec.get("value"), "ts": ts})
    elif kind == "notice" and rec.get("level") in ("error", "warn"):
        ep.notices.append({"level": rec.get("level"), "text": str(rec.get("text") or "")[:300], "ts": ts})
    elif kind == "llm_interrupted":
        ep.notices.append({"level": "interrupted", "text": "", "ts": ts})


def _call(ep: Episode, cid) -> dict | None:
    for c in reversed(ep.calls):
        if c.get("id") == cid:
            return c
    return None


def _program(ep: Episode, run_id: str, ts: float) -> dict:
    for p in ep.programs:
        if p["run_id"] == run_id:
            return p
    p = {"run_id": run_id, "ts": ts, "progname": "", "source": "", "static": [], "syntax": None,
         "audit": None, "can_run": None, "confirmed": False, "run": None, "fixes": [],
         "exemplar_id": "", "origin": None, "attempt": len(ep.programs) + 1}
    ep.programs.append(p)
    return p


def _program_event(ep: Episode, kind: str, rec: dict, ts: float) -> None:
    run_id = str(rec.get("run_id") or "")
    if kind == "catalogue_replay":
        ep.origin = {"catalogue": rec.get("note"), "values": rec.get("values") or {}}
        return
    if not run_id:
        return
    p = _program(ep, run_id, ts)
    if kind == "code_checked":
        p["progname"] = rec.get("progname", "")
        p["static"] = list(rec.get("static_problems") or [])
    elif kind == "code_ready":
        p["source"] = str(rec.get("source") or "")
        p["can_run"] = rec.get("can_run")
        if rec.get("syntax") is not None:
            p["syntax"] = rec.get("syntax")
        if rec.get("audit") is not None:
            p["audit"] = rec.get("audit")
        if rec.get("origin"):
            p["origin"] = rec.get("origin")
    elif kind == "syntax_error":
        p["syntax"] = {"ok": False, "message": rec.get("message", ""), "line": rec.get("line"),
                       "word": rec.get("word", ""), "attempt": rec.get("attempt")}
    elif kind == "audit":
        p["audit"] = {k: rec.get(k) for k in ("allowed", "risk", "violations", "summary", "unavailable")
                      if k in rec}
    elif kind == "code_confirmed":
        p["confirmed"] = True
    elif kind == "run_started":
        p["run"] = {"job": rec.get("job", ""), "mode": rec.get("mode", ""), "started": ts}
    elif kind == "run_result":
        run = p["run"] or {}
        run.update({"ok": bool(rec.get("ok")), "rc": rec.get("rc"), "message": rec.get("message", ""),
                    "lines": list(rec.get("lines") or []), "ended": ts})
        p["run"] = run
    elif kind == "run_aborted":
        run = p["run"] or {}
        run.update({"ok": False, "rc": -1, "message": "aborted: " + str(rec.get("text") or ""),
                    "ended": ts})
        p["run"] = run
    elif kind == "code_discarded":
        p["discarded"] = True
    elif kind == "exemplar_saved":
        p["exemplar_id"] = str(rec.get("note") or "")
        ep.exemplar_id = p["exemplar_id"]


def _finish(ep: Episode) -> None:
    if ep.outcome == "command":
        return
    programs = ep.programs
    runs = [p for p in programs if p.get("run") and "ok" in (p.get("run") or {})]
    if runs:
        last = runs[-1]
        ep.outcome = "confirmed_ran_ok" if last["run"].get("ok") else "ran_failed"
        return
    if any(p.get("discarded") for p in programs):
        ep.outcome = "discarded"
        return
    if any(n["level"] == "interrupted" for n in ep.notices) and not programs:
        ep.outcome = "interrupted"
        return
    if programs:
        audits = [p.get("audit") for p in programs if p.get("audit")]
        if audits and all(a.get("allowed") is False for a in audits):
            ep.outcome = "denied"
        elif any(p.get("can_run") for p in programs) or any(
                p.get("audit") and p["audit"].get("allowed") for p in programs):
            ep.outcome = "abandoned_code"
        else:
            ep.outcome = "no_program"
        return
    if ep.calls:
        ep.outcome = "answered"
        return
    ep.outcome = "empty"
