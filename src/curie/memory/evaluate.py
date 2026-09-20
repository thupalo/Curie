"""The evaluation loop (knowledge plan, section 10): archived questions
replayed through the real turn, nothing executed, the outcome measured.

    cases = question_bank(archive)                # confirmed episodes
    result = await evaluator.run(cases)           # one turn per case
    report_markdown(result) -> knowledge/EVAL.md

A case is an episode that ended in a confirmed, successful run: the
question and the program the user accepted. The evaluator opens a dev
session, sends the question, lets the model use its tools, lets Curie
check the program (static rules, SAP syntax check, audit) and stops
before the Run button (ABAP_REQUIRE_CONFIRM is forced on). Measured per
case:

    replayed          a family or catalogue template answered without the model
    attempts          program versions until one passed the checks (or gave up)
    first_syntax_ok   the first program passed the SAP syntax check
    runnable          the final program could be run (rules, syntax, audit)
    audit             the final verdict's risk, or "denied"
    shape_equal       the final program's shape equals the accepted one's
    tables_equal      it reads the same tables
    model_calls, tool_calls, tool_errors, seconds, tokens, context_chars

The summary is kept in the archive (eval_runs) so the next run reports
the deltas; the report goes to knowledge/EVAL.md next to REVIEW.md.
Nothing learned during an evaluation reaches the notes: no program runs,
so no exemplar, insight or counter changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from curie import config, lexicon
from curie.memory import shape as sh

log = logging.getLogger(__name__)

EVAL_FILE = "EVAL.md"
# Questions that ask for a program: a tools-only answer is a failure there.
# Language-dependent: the wording comes from curie/lexicon.py.
PROGRAM_REQUEST = lexicon.regex("program_request")


def asks_for_program(text: str) -> bool:
    return bool(PROGRAM_REQUEST.search(text or ""))


@dataclass
class Case:
    id: str
    question: str
    program: str
    tables: list = field(default_factory=list)
    lines: int = 0
    refinements: list = field(default_factory=list)   # the thread's follow-ups, sent as further turns
    general: bool = False     # the opening question was answered without asking SAP
    borrowed: list = field(default_factory=list)      # filter values of the program absent from the question

    @property
    def shape(self) -> str:
        return sh.analyse(self.program).signature()

    @property
    def excluded_reason(self) -> str:
        if self.general:
            return "opening question answered without asking SAP"
        if self.borrowed:
            return "accepted program uses values that are not in the question: " + ", ".join(self.borrowed)
        return ""


def borrowed_values(question: str, program: str) -> list[str]:
    """Filter literals of the program that look like identifiers or dates
    (four digits or more) and that no value of the question renders: the
    model took them from an earlier thread. Such a case cannot be
    reproduced from its question and is excluded from the evaluation."""
    shape = sh.analyse(program or "")
    values = sh.question_values(question or "")
    out: list[str] = []
    for lit in shape.literals:
        value = (lit.value or "").strip()
        if lit.op == "UP TO" or not value.isdigit() or len(value) < 4 or not value.strip("0"):
            continue                      # an initial date or number ('00000000') is a constant
        if any(v.renders(value) for v in values):
            continue
        if value not in out:
            out.append(value)
    return out


def opening_without_sap(archive, episode_id: str) -> bool:
    """True when the episode's first turn (up to the first follow-up) ended
    with an answer and no tool call and no program: a general question
    ("how do I see a customer balance in SAP GUI") whose thread only later
    turned to the system's data. Such a case measures nothing about the
    data path and is excluded from the evaluation."""
    rows = archive.query(
        "SELECT ts, data FROM events WHERE episode_id = ? AND kind = 'user_message' ORDER BY ts", (episode_id,))
    if len(rows) < 2:
        return False                      # one turn: the program came from the question itself
    boundary = float(rows[1]["ts"] or 0)
    # The tool_calls table, not calls.tool_calls: logs from before the
    # llm_done record carry the tool records but no count per call.
    tools = archive.query("SELECT COUNT(*) AS n FROM tool_calls WHERE episode_id = ? AND ts < ?",
                          (episode_id, boundary))
    if tools and int(tools[0]["n"] or 0) > 0:
        return False
    programs = archive.query("SELECT COUNT(*) AS n FROM programs WHERE episode_id = ? AND ts < ?",
                             (episode_id, boundary))
    return not (programs and int(programs[0]["n"] or 0) > 0)


@dataclass
class CaseResult:
    case: Case
    replayed: str = ""
    attempts: int = 0
    first_syntax_ok: bool | None = None
    runnable: bool = False
    audit: str = ""
    shape_equal: bool = False
    tables_equal: bool = False
    tables: list = field(default_factory=list)
    model_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    seconds: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    context_chars: int = 0
    error: str = ""
    repairs: int = 0            # deterministic repairs applied before the checks
    audit_memo: bool = False    # the audit verdict came from the memo, no second model call
    session: str = ""           # the eval session's id: curie --trace <id> shows its timeline

    @property
    def program_expected(self) -> bool:
        return any(asks_for_program(t) for t in [self.case.question] + list(self.case.refinements))

    @property
    def program_missing(self) -> bool:
        return self.program_expected and self.attempts == 0

    def row(self) -> dict:
        return {"id": self.case.id, "question": self.case.question[:120], "replayed": self.replayed,
                "program_expected": self.program_expected, "program_missing": self.program_missing,
                "attempts": self.attempts, "first_syntax_ok": self.first_syntax_ok,
                "runnable": self.runnable, "audit": self.audit, "shape_equal": self.shape_equal,
                "tables_equal": self.tables_equal, "model_calls": self.model_calls,
                "tool_calls": self.tool_calls, "tool_errors": self.tool_errors,
                "seconds": round(self.seconds, 1), "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "context_chars": self.context_chars, "error": self.error,
                "repairs": self.repairs, "audit_memo": self.audit_memo, "session": self.session}


def question_bank(archive, limit: int = 0, ids: list[str] | None = None, include_general: bool = False,
                  excluded: list | None = None) -> list[Case]:
    """Confirmed episodes with their accepted program, newest first. Cases
    whose opening question was answered without SAP, or whose program uses
    values the question does not carry, are left out ({"id", "reason"}
    entries go to `excluded` when a list is given) unless `include_general`."""
    rows = archive.query(
        "SELECT e.id, e.question, e.refinements, p.source, p.tables, p.lines FROM episodes e "
        "JOIN programs p ON p.episode_id = e.id "
        "WHERE e.outcome = 'confirmed_ran_ok' AND p.run_ok = 1 AND e.question <> '' "
        "ORDER BY e.started DESC, p.ts DESC")
    seen: set[str] = set()
    out: list[Case] = []
    for r in rows:
        if r["id"] in seen or (ids and r["id"] not in ids):
            continue
        seen.add(r["id"])
        case = Case(id=r["id"], question=r["question"], program=r["source"] or "",
                    tables=json.loads(r["tables"] or "[]"), lines=int(r["lines"] or 0),
                    refinements=[str(x) for x in json.loads(r["refinements"] or "[]")])
        if case.refinements:
            case.general = opening_without_sap(archive, case.id)
        if not case.general:
            case.borrowed = borrowed_values(" ".join([case.question] + case.refinements), case.program)
        if case.excluded_reason and not include_general:
            if excluded is not None:
                excluded.append({"id": case.id, "reason": case.excluded_reason})
            continue
        out.append(case)
        if limit and len(out) >= limit:
            break
    return out


class Evaluator:
    def __init__(self, manager, orchestrator, knowledge=None, llm=None):
        self.manager = manager
        self.orchestrator = orchestrator
        self.knowledge = knowledge
        self.llm = llm

    async def run_case(self, case: Case, timeout: float = 600.0) -> CaseResult:
        result = CaseResult(case=case)
        session = self.manager.open_dev("", user="EVAL")
        result.session = str(getattr(session, "id", "") or "")
        started = time.monotonic()
        try:
            if self.knowledge is not None and hasattr(self.knowledge, "context_for"):
                result.context_chars = len(self.knowledge.context_for(case.question, [], "").text)
            # The thread as the user lived it: the question, then each
            # follow-up as its own turn (a program waiting for confirmation
            # is dropped by the next prompt, as in the browser).
            for text in [case.question] + list(case.refinements):
                for run in session.runs.values():
                    if run.status in ("ready", "blocked"):
                        run.status = "discarded"
                if session.turn_task is not None and not session.turn_task.done():
                    break
                if not self.orchestrator.start_turn(session, text):
                    result.error = "turn not started"
                    break
                try:
                    # One budget for the whole case, not per turn: the
                    # follow-ups share what the first turn left.
                    remaining = max(0.01, started + timeout - time.monotonic())
                    await asyncio.wait_for(asyncio.shield(session.turn_task), remaining)
                except asyncio.TimeoutError:
                    result.error = f"timeout after {timeout:.0f}s"
                    self.orchestrator.stop(session)
                    break
                except Exception as exc:  # noqa: BLE001
                    result.error = f"{type(exc).__name__}: {exc}"
                    break
            result.seconds = time.monotonic() - started
            self._measure(session, case, result)
        finally:
            for run in session.runs.values():
                if run.status in ("ready", "blocked"):
                    run.status = "discarded"
            self.manager.close(session, "eval")
        return result

    def _measure(self, session, case: Case, result: CaseResult) -> None:
        runs = sorted(session.runs.values(), key=lambda r: r.created)
        result.attempts = len(runs)
        for event, data in session.transcript:
            if event == "assistant_start":
                result.model_calls += 1
            elif event == "tool_call":
                result.tool_calls += 1
            elif event == "tool_result" and not data.get("ok"):
                result.tool_errors += 1
        if runs:
            first, last = runs[0], runs[-1]
            result.repairs = sum(len(getattr(r, "repairs", []) or []) for r in runs)
            result.audit_memo = any(bool((r.audit or {}).get("memo")) for r in runs)
            if first.syntax is not None:
                result.first_syntax_ok = bool(first.syntax.get("ok"))
            result.runnable = bool(last.can_run)
            if last.audit:
                result.audit = "denied" if last.audit.get("allowed") is False else str(last.audit.get("risk", ""))
            if last.origin:
                result.replayed = last.origin.get("family") or last.origin.get("catalogue") or ""
            produced = sh.analyse(last.source)
            result.tables = produced.tables
            result.shape_equal = produced.signature() == case.shape
            result.tables_equal = produced.tables == sorted(t.upper() for t in case.tables)
        # Tokens are in the session log's llm_done records (logs/sessions/eval),
        # not in the transcript; the report leaves them at zero.

    async def run(self, cases: list[Case], timeout: float = 600.0) -> dict:
        results: list[CaseResult] = []
        started = time.time()
        for i, case in enumerate(cases, 1):
            log.info("eval %d/%d %s: %s", i, len(cases), case.id, case.question[:80])
            r = await self.run_case(case, timeout=timeout)
            log.info("   -> %s attempts %d, syntax first %s, runnable %s, audit %s, shape %s, "
                     "tools %d (%d errors), %.0fs%s",
                     "replayed " + r.replayed if r.replayed else "generated", r.attempts,
                     r.first_syntax_ok, r.runnable, r.audit or "-", "=" if r.shape_equal else "!=",
                     r.tool_calls, r.tool_errors, r.seconds, f", error {r.error}" if r.error else "")
            results.append(r)
        return {"started": started, "seconds": round(time.time() - started, 1),
                "model": getattr(self.llm, "model", ""), "cases": [r.row() for r in results],
                "summary": summarize(results)}


def summarize(results: list[CaseResult]) -> dict:
    n = len(results)
    if not n:
        return {"cases": 0}
    checked = [r for r in results if r.first_syntax_ok is not None]
    return {
        "cases": n,
        "replayed": sum(1 for r in results if r.replayed),
        "runnable": sum(1 for r in results if r.runnable),
        "first_syntax_ok": sum(1 for r in checked if r.first_syntax_ok),
        "syntax_checked": len(checked),
        "denied": sum(1 for r in results if r.audit == "denied"),
        "shape_equal": sum(1 for r in results if r.shape_equal),
        "tables_equal": sum(1 for r in results if r.tables_equal),
        "errors": sum(1 for r in results if r.error),
        "program_expected": sum(1 for r in results if r.program_expected),
        "program_missing": sum(1 for r in results if r.program_missing),
        "repairs": sum(r.repairs for r in results),
        "audit_memo": sum(1 for r in results if r.audit_memo),
        "replayed_wrong": sum(1 for r in results if r.replayed and not r.shape_equal),
        "attempts_avg": round(sum(r.attempts for r in results) / n, 2),
        "model_calls_avg": round(sum(r.model_calls for r in results) / n, 2),
        "tool_calls_avg": round(sum(r.tool_calls for r in results) / n, 2),
        "tool_errors": sum(r.tool_errors for r in results),
        "seconds_avg": round(sum(r.seconds for r in results) / n, 1),
        "context_chars_avg": int(sum(r.context_chars for r in results) / n),
    }


def report_markdown(result: dict, previous: dict | None = None) -> str:
    s = result["summary"]
    when = datetime.fromtimestamp(result["started"]).strftime("%Y-%m-%d %H:%M")
    lines = [f"# Evaluation, {when}", "",
             f"{s.get('cases', 0)} case(s) from the archive, model `{result.get('model') or '?'}`, "
             f"{result.get('seconds', 0)} s.", ""]
    excluded = result.get("excluded") or []
    if excluded:
        lines.append(f"{len(excluded)} case(s) excluded (`--include-general` replays them):")
        for e in excluded[:20]:
            if isinstance(e, dict):
                lines.append(f"- {e.get('id', '?')}: {e.get('reason', '')}")
            else:
                lines.append(f"- {e}")
        lines.append("")
    prev = (previous or {}).get("summary") or {}
    lines.append("| Measure | Now | Previous |")
    lines.append("|---|---|---|")
    for key, label in (("runnable", "runnable after the checks"), ("first_syntax_ok", "syntax ok at the first attempt"),
                       ("shape_equal", "same shape as the accepted program"), ("tables_equal", "same tables"),
                       ("replayed", "replayed from a family or the catalogue"),
                       ("replayed_wrong", "replayed with a different shape than the accepted program"),
                       ("denied", "denied by the audit"),
                       ("program_missing", "program asked for but none written"),
                       ("audit_memo", "audit verdict reused from the memo"),
                       ("repairs", "deterministic repairs before the checks"),
                       ("errors", "errors or timeouts"), ("attempts_avg", "program versions per case"),
                       ("model_calls_avg", "model calls per case"), ("tool_calls_avg", "tool calls per case"),
                       ("tool_errors", "tool errors"), ("seconds_avg", "seconds per case"),
                       ("context_chars_avg", "knowledge characters in the prompt")):
        now = s.get(key, "")
        if key in ("runnable", "first_syntax_ok", "shape_equal", "tables_equal", "replayed", "denied", "errors",
                   "program_missing", "audit_memo", "replayed_wrong"):
            total_key = {"first_syntax_ok": "syntax_checked", "program_missing": "program_expected",
                         "replayed_wrong": "replayed"}.get(key, "cases")
            now = f"{now} / {s.get(total_key, 0)}"
            before = f"{prev.get(key, '')} / {prev.get(total_key, '')}" if prev and key in prev else "-"
        else:
            before = prev.get(key, "-") if prev else "-"
        lines.append(f"| {label} | {now} | {before} |")
    lines += ["", "## Cases", "",
              "| Case | Question | Replayed | Attempts | Syntax 1st | Runnable | Audit | Shape | Tables | Tools | s | Log |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in result["cases"]:
        missing = " (program expected)" if c.get("program_missing") else ""
        lines.append(f"| {c['id']} | {c['question'][:70].replace('|', '/')} | {c['replayed'] or '-'} | "
                     f"{c['attempts']}{missing} | {_yn(c['first_syntax_ok'])} | {_yn(c['runnable'])} | {c['audit'] or '-'} | "
                     f"{_yn(c['shape_equal'])} | {_yn(c['tables_equal'])} | "
                     f"{c['tool_calls']}{'/' + str(c['tool_errors']) + ' err' if c['tool_errors'] else ''} | "
                     f"{c['seconds']}{' ' + c['error'] if c['error'] else ''} | "
                     f"{(c.get('session') or '')[:8] or '-'} |")
    lines += ["", "Run `curie --eval` after a change to prompts, notes, retrieval or the bundle; "
              "the previous column comes from the archive's last run. `curie --trace <Log>` prints "
              "a case's timeline."]
    return "\n".join(lines) + "\n"


def _yn(value) -> str:
    if value is None:
        return "?"
    return "yes" if value else "no"


def write_report(text: str, knowledge_dir: str | Path) -> Path:
    path = Path(knowledge_dir) / EVAL_FILE
    path.write_text(text, encoding="utf-8")
    return path
