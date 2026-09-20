"""The code path: extract -> rules -> syntax check -> audit -> confirm -> run.

Called by the orchestrator with a final answer; runs inside the same turn
task, so Stop cancels it too. After the user's confirmation, execute() runs
the program as its own task and feeds the list back to the model.
"""
from __future__ import annotations

import asyncio
import logging

from curie import config
from curie.llm import audit
from curie.llm.client import LLMError, parse_json_object
from curie.llm.prompts import (INSIGHT_PROMPT, THREAD_PROMPT, insight_prompt, result_prompt,
                               rules_fix_prompt, syntax_fix_prompt, thread_prompt)
from curie.memory.threads import compose_question
from curie.sap_tools.abap_source import (dialect_problems, extract_abap_blocks, new_progname,
                                         prepare_source, repair_source, sanitize_for_codepage)
from curie.sessions.model import AbapRun, Session, State

log = logging.getLogger(__name__)

RESULT_MAX_LINES = 300
RESULT_MAX_CHARS = 12000
INSIGHT_MAX_FIXES = 2      # fix rounds distilled per accepted program
INSIGHT_DIFF_LINES = 8


def source_diff(before: str, after: str) -> tuple[list[str], list[str]]:
    """Lines only in `before` and only in `after`, order kept, REPORT line ignored."""
    old = [l for l in before.split("\n") if not l.upper().startswith("REPORT ")]
    new = [l for l in after.split("\n") if not l.upper().startswith("REPORT ")]
    old_set, new_set = set(old), set(new)
    removed = [l for l in old if l not in new_set and l.strip()][:INSIGHT_DIFF_LINES]
    added = [l for l in new if l not in old_set and l.strip()][:INSIGHT_DIFF_LINES]
    return removed, added


class AbapFlow:
    def __init__(self, manager, executor, llm, orchestrator, knowledge=None, audit_memo=None):
        self.manager = manager
        self.executor = executor
        self.llm = llm
        self.orchestrator = orchestrator
        self.knowledge = knowledge  # memory.KnowledgeStore or None
        self.audit_memo = audit_memo  # memory.audit_memo.AuditMemo or None

    # -- after an answer ------------------------------------------------------------ #

    async def replay(self, session: Session, text: str) -> bool:
        """The report catalogue: a program the user marked correct for the
        same question shape is replayed without the model, values of the new
        question substituted. Still goes through rules, syntax check, audit
        and the Run button. False when nothing matches."""
        if self.knowledge is None:
            return False
        routed = False
        try:
            match = self._find_template(text)
            if match is None and config.KNOWLEDGE_ROUTER and self.llm is not None:
                router = getattr(self.knowledge, "route_family", None)
                if router is not None:
                    m = await router(text, self.llm)
                    if m is not None:
                        match = (m.note, m.program, dict(m.values), m.kind)
                        routed = True
        except Exception as exc:  # noqa: BLE001
            log.warning("catalogue lookup failed: %s", exc)
            return False
        if match is None:
            return False
        note, program, values, kind = match
        mgr = self.manager
        # The user's message is already in the browser and the thread is
        # set up (Orchestrator.open_thread); the replay only adds what it
        # used to the thread's record.
        session.messages.append({"role": "user", "content": text})
        session.last_prompt = text
        session.thread_tables = set(note.tables)
        session.injected_notes = []
        session.injected_exemplars = [note.id]
        session.thread_notes = []
        session.thread_exemplars = [note.id]
        session.fixes = []
        shown = ", ".join(f"{k} = {v}" for k, v in values.items()) or "the same values"
        mgr.log_llm(session, "catalogue_replay", {"note": note.id, "values": values, "kind": kind,
                                                  "routed": routed})
        what = "report family" if kind == "family" else "report catalogue"
        how = "the model matched this question to" if routed else "this question matches"
        mgr.notice(session, f"{what.capitalize()}: {how} {note.id} "
                            f"(confirmed by {note.author or 'a user'}, {note.created}); "
                            f"replaying the reviewed program with {shown}. It still goes "
                            "through the checks and needs your confirmation.")
        session.messages.append({"role": "assistant", "content":
                                 f"[Curie replayed the {what} program for \"{note.question}\" "
                                 f"with {shown}; it is shown in the code card.]\n"
                                 f"```abap\n{program}\n```"})
        origin = {kind if kind == "family" else "catalogue": note.id, "values": values,
                  "audit": note.audit if not values else None}
        await self.handle_answer(session, f"```abap\n{program}\n```", origin=origin)
        return True

    def _find_template(self, text: str):
        """(note, program, values, kind) through the facade, or through a
        bare store's catalogue; None when nothing matches."""
        finder = getattr(self.knowledge, "match_family", None)
        if finder is not None:
            m = finder(text)
            if m is None:
                return None
            return m.note, m.program, dict(m.values), m.kind
        found = self.knowledge.find_template(text)
        if found is None:
            return None
        note, mapping = found
        return note, self.knowledge.instantiate(note, mapping), dict(mapping), "catalogue"

    async def handle_answer(self, session: Session, answer: str, attempt: int = 0,
                            origin: dict | None = None) -> None:
        blocks = extract_abap_blocks(answer)
        if not blocks:
            return
        if len(blocks) > 1:
            self.manager.notice(session, "The answer contains several ABAP blocks; "
                                         "only the first one is checked.", "warn")
        code = blocks[0]
        mgr = self.manager
        mgr.set_state(session, State.CHECKING, "Checking the ABAP code")

        progname = new_progname()
        lines, problems = prepare_source(code, progname, self.executor.effective_width())
        # Characters the backend codepage cannot carry are replaced, not
        # rejected: a dash in a comment is no reason for a fix round trip.
        lines, replaced = sanitize_for_codepage(lines)
        if replaced:
            mgr.notice(session, "Characters outside the SAP codepage were replaced: "
                                + "; ".join(replaced[:5])
                                + (" ..." if len(replaced) > 5 else ""), "info")
        # The repairs the archive justified (a period before ENDIF, an
        # indented comment, UP TO after WHERE) cost no round trip; the user
        # sees each one. Then the constructs this release cannot compile
        # are named before SAP is asked.
        repairs: list[str] = []
        if config.ABAP_REPAIR:
            lines, repairs = repair_source(lines)
            if repairs:
                mgr.notice(session, "Repaired before the check: " + "; ".join(repairs[:6])
                                    + (" ..." if len(repairs) > 6 else ""), "info")
        problems += dialect_problems(lines)
        run = mgr.new_run(session, progname, "\n".join(lines))
        run.fixes = list(session.fixes)
        run.repairs = repairs
        run.thread_id = session.thread_id
        if origin:
            run.origin = {"catalogue": origin.get("catalogue"), "values": origin.get("values") or {}}
            if origin.get("family"):
                run.origin["family"] = origin["family"]
        problems += audit.static_check(lines)
        mgr.log_llm(session, "code_checked", {"run_id": run.run_id, "progname": progname,
                                              "static_problems": problems, "repairs": repairs})
        if problems:
            run.status = "blocked"
            session.fixes.append({"kind": "rules", "message": "; ".join(problems), "word": "",
                                  "line": 0, "source": run.source})
            mgr.emit(session, "code_ready", {
                "run_id": run.run_id, "progname": progname, "source": run.source,
                "syntax": None, "audit": {"allowed": False, "risk": "high",
                                          "violations": problems,
                                          "summary": "rejected by Curie's static rules"},
                "can_run": False, "origin": run.origin,
            })
            await self._retry(session, rules_fix_prompt(problems, attempt + 1, config.ABAP_FIX_RETRIES),
                              attempt)
            return

        syntax = await self.executor.check(session, run, lines)
        run.syntax = syntax
        if syntax.get("infra"):
            # Curie's own check path is broken (FM missing, dumping, connection
            # lost): tell the user, do not ask the model to "fix" its code.
            run.status = "blocked"
            mgr.emit(session, "code_ready", {
                "run_id": run.run_id, "progname": progname, "source": run.source,
                "syntax": syntax, "audit": None, "can_run": False, "origin": run.origin,
            })
            mgr.notice(session, f"The program could not be checked in SAP: {syntax['message']}",
                       "error")
            session.messages.append({"role": "user", "content":
                                     "[Curie could not check the program in SAP because of an "
                                     "infrastructure problem; do not change the code because of this.]"})
            return
        if not syntax["ok"]:
            run.status = "blocked"
            session.fixes.append({"kind": "syntax", "message": syntax["message"],
                                  "word": syntax["word"], "line": syntax["line"],
                                  "source": run.source})
            mgr.emit(session, "syntax_error", {
                "run_id": run.run_id, "message": syntax["message"], "line": syntax["line"],
                "word": syntax["word"], "attempt": attempt + 1,
                "max_attempts": config.ABAP_FIX_RETRIES,
            })
            mgr.emit(session, "code_ready", {
                "run_id": run.run_id, "progname": progname, "source": run.source,
                "syntax": syntax, "audit": None, "can_run": False, "origin": run.origin,
            })
            await self._retry(session, syntax_fix_prompt(syntax, attempt + 1, config.ABAP_FIX_RETRIES),
                              attempt)
            return

        verdict = None
        if config.ABAP_AUDIT:
            stored = (origin or {}).get("audit")
            if stored and "allowed" in stored:
                # Replayed unchanged from the catalogue: the verdict given when
                # the user confirmed it still holds for the same program.
                verdict = {"allowed": bool(stored["allowed"]), "risk": stored.get("risk", "low"),
                           "violations": [], "summary": f"{stored.get('summary', '')} "
                           f"(catalogue verdict, unchanged program)".strip()}
            else:
                memo = self.audit_memo.lookup(run.source) if self.audit_memo is not None else None
                if memo is not None:
                    # The same program shape was allowed before (and the
                    # user confirmed it): the verdict holds, the card says so.
                    verdict = memo
                else:
                    mgr.set_state(session, State.CHECKING, "Security review of the code")
                    verdict = await audit.llm_audit(self.llm, run.source)
                    if self.audit_memo is not None:
                        self.audit_memo.remember(run.source, verdict, run.run_id)
            run.audit = verdict
            mgr.log_llm(session, "audit", {"run_id": run.run_id, **verdict})
        can_run = verdict is None or verdict["allowed"] or not config.ABAP_AUDIT_BLOCKS
        run.can_run = can_run
        run.status = "ready" if can_run else "blocked"
        mgr.emit(session, "code_ready", {
            "run_id": run.run_id, "progname": progname, "source": run.source,
            "syntax": syntax, "audit": verdict, "can_run": can_run, "origin": run.origin,
        })
        if can_run:
            if config.ABAP_REQUIRE_CONFIRM:
                mgr.set_state(session, State.CONFIRM, "Waiting for confirmation in the browser")
            else:
                await self.execute(session, run)
        else:
            mgr.notice(session, "The security review did not allow this program to run.", "warn")

    async def _retry(self, session: Session, prompt: str, attempt: int) -> None:
        if attempt + 1 > config.ABAP_FIX_RETRIES:
            self.manager.notice(session, f"No valid program after {config.ABAP_FIX_RETRIES} "
                                         "attempts; please rephrase the request.", "warn")
            return
        answer = await self.orchestrator.run_turn(session, prompt, auto=True)
        if answer:
            await self.handle_answer(session, answer, attempt + 1)

    # -- confirmation ------------------------------------------------------------------ #

    def confirm(self, session: Session, run_id: str) -> bool:
        run = session.runs.get(run_id)
        if run is None or run.status != "ready" or session.closed:
            return False
        if session.turn_task is not None and not session.turn_task.done():
            return False
        session.turn_task = asyncio.create_task(self._execute_task(session, run),
                                                name=f"run-{run.run_id}")
        return True

    def discard(self, session: Session, run_id: str) -> bool:
        run = session.runs.get(run_id)
        if run is None or run.status not in ("ready", "blocked"):
            return False
        run.status = "discarded"
        session.messages.append({"role": "user",
                                 "content": "[The user discarded the program without running it.]"})
        self.manager.log_llm(session, "code_discarded", {"run_id": run_id})
        if session.state is State.CONFIRM:
            self.manager.set_state(session, State.IDLE, "")
        return True

    def stop_run(self, session: Session) -> bool:
        """Stop button while a program runs: cancel the wait, abort the job."""
        running = [r for r in session.runs.values() if r.status == "running"]
        if not running:
            return False
        task = session.turn_task
        if task is not None and not task.done():
            task.cancel()
        for run in running:
            run.status = "aborted"
            asyncio.create_task(self._abort(session, run), name=f"abort-{run.run_id}")
        return True

    async def _abort(self, session: Session, run: AbapRun) -> None:
        try:
            text = await self.executor.abort(run)
        except Exception as exc:  # noqa: BLE001
            log.exception("abort failed")
            text = f"stop requested, cleanup failed: {exc}"
        run.result = {"ok": False, "rc": -1, "message": f"Stopped by the user: {text}", "lines": []}
        self.manager.emit(session, "run_result", {"run_id": run.run_id, **run.result})
        self.manager.log_llm(session, "run_aborted", {"run_id": run.run_id, "text": text})
        session.messages.append({"role": "user",
                                 "content": f"[The user stopped the program {run.progname}: {text}.]"})
        if session.state not in (State.CONFIRM, State.CLOSED):
            self.manager.set_state(session, State.IDLE, "")

    async def _execute_task(self, session: Session, run: AbapRun) -> None:
        try:
            await self.execute(session, run)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("run failed")
            self.manager.notice(session, f"Internal error: {type(exc).__name__}: {exc}", "error")
        finally:
            if session.state not in (State.CONFIRM, State.CLOSED):
                self.manager.set_state(session, State.IDLE, "")

    async def execute(self, session: Session, run: AbapRun) -> None:
        mgr = self.manager
        run.status = "running"
        mgr.set_state(session, State.RUNNING, f"Running {run.progname} in SAP")
        mgr.log_llm(session, "code_confirmed", {"run_id": run.run_id, "progname": run.progname})
        lines = run.source.split("\n")

        def started(r: AbapRun):
            mgr.emit(session, "run_started", {"run_id": r.run_id, "job": r.job, "mode": r.mode})
            if r.job:
                mgr.set_state(session, State.RUNNING, f"Job {r.job} running")

        result = await self.executor.execute(session, run, lines, on_started=started)
        run.result = result
        run.status = "done" if result.get("ok") else ("running" if result.get("pending") else "failed")
        mgr.emit(session, "run_result", {
            "run_id": run.run_id, "ok": bool(result.get("ok")), "rc": result.get("rc", 0),
            "message": result.get("message", ""), "lines": result.get("lines", []),
        })
        await self._learn_from_run(session, run, bool(result.get("ok")), pending=bool(result.get("pending")))
        if result.get("ok"):
            await self._feed_back(session, run, result.get("lines", []))
        else:
            session.messages.append({"role": "user", "content":
                                     f"[The program {run.progname} did not run successfully: "
                                     f"{result.get('message', '')}]"})

    async def _learn_from_run(self, session: Session, run: AbapRun, ok: bool, pending: bool = False) -> None:
        """Credit or debit the notes that were injected, and keep a confirmed
        program that ran as an exemplar for similar questions."""
        if self.knowledge is None or pending:
            return
        try:
            # The run result is the thread's outcome (one per thread; the
            # notes the programs and answers used get the credit).
            credit = getattr(self.orchestrator, "credit_thread", None)
            if credit is not None:
                credit(session, ok, "run")
            else:
                self.knowledge.record(session.injected_exemplars, success=ok)
                self.knowledge.record(session.injected_notes, success=ok)
            if ok and config.KNOWLEDGE_CAPTURE_EXEMPLARS and session.last_prompt and not run.origin:
                if len(session.thread) > 1:
                    # The question is rewritten by the model: do not hold the result up.
                    asyncio.create_task(self._capture_exemplar(session, run), name=f"capture-{run.run_id}")
                else:
                    await self._capture_exemplar(session, run)
            if ok and config.KNOWLEDGE_CAPTURE_INSIGHTS and run.fixes:
                asyncio.create_task(self._learn_insights(session, run), name=f"insights-{run.run_id}")
        except Exception as exc:  # noqa: BLE001 -- learning must never break a run
            log.warning("knowledge update failed: %s", exc)

    async def _capture_exemplar(self, session: Session, run: AbapRun) -> None:
        """Keep the accepted program under the thread's business question.

        With follow-ups in the thread the model rewrites the question with
        the refinements folded in and may propose a preference worth keeping
        as a hint ("leave out the XYZ* columns"). A later run in the same
        thread replaces the thread's exemplar instead of adding one."""
        thread = list(session.thread) or [session.last_prompt]
        question, hint = thread[0], ""
        refinements = thread[1:]
        if refinements:
            question = compose_question(question, refinements)
            messages = [{"role": "system", "content": THREAD_PROMPT},
                        {"role": "user", "content": thread_prompt(thread, run.source)}]
            try:
                text = await self.llm.complete(messages, model=config.LLM_AUDIT_MODEL, temperature=0.0,
                                               json_mode=True,
                                               max_tokens=max(int(config.LLM_MAX_OUTPUT_TOKENS), 1024))
                obj = parse_json_object(text) or {}
                rewritten = " ".join(str(obj.get("question", "")).split())
                if 12 <= len(rewritten) <= 300:
                    question = rewritten
                hint = " ".join(str(obj.get("hint") or "").split())
                if hint.lower() in ("null", "none") or len(hint) < 12:
                    hint = ""
            except LLMError as exc:
                log.warning("thread question rewrite failed: %s", exc)
        try:
            note = self.knowledge.add_exemplar(question, run.source, session.sap.user, run.run_id,
                                               audit=run.audit, refinements=refinements,
                                               replace=session.thread_exemplar_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("exemplar save failed: %s", exc)
            return
        if note is None:
            return
        run.exemplar_id = note.id
        replaced = session.thread_exemplar_id == note.id
        session.thread_exemplar_id = note.id
        self.manager.log_llm(session, "exemplar_saved", {"note": note.id, "run_id": run.run_id,
                                                         "question": question, "replaced": replaced})
        what = "Updated" if replaced else "Kept this program as"
        self.manager.notice(session, f"{what} {note.id} for the question \"{question[:120]}\" "
                                     "(/forget to retire it; mark the result correct to add it to "
                                     "the report catalogue).")
        if run.wants_catalogue and not note.catalogue:
            self.knowledge.mark_catalogue(note.id, on=True)
            self.manager.notice(session, f"{note.id} is now in the report catalogue.")
        if hint:
            try:
                h = self.knowledge.add_hint(hint, session.sap.user, note.tables)
                self.manager.log_llm(session, "hint_from_thread", {"note": h.id, "text": hint})
                self.manager.notice(session, f"Remembered your preference as {h.id}: {hint} "
                                             f"(/forget {h.id} if it was only for this program).")
            except Exception as exc:  # noqa: BLE001
                log.warning("hint save failed: %s", exc)

    async def _learn_insights(self, session: Session, run: AbapRun) -> None:
        """Distil the fix rounds an accepted program went through into
        one-line pitfalls (layer three). Fail-closed: no JSON, NONE or a
        short text means nothing is learned."""
        from curie.memory.store import program_tables

        budget = max(int(config.LLM_MAX_OUTPUT_TOKENS), 1024)
        for fix in run.fixes[:INSIGHT_MAX_FIXES]:
            removed, added = source_diff(fix.get("source", ""), run.source)
            messages = [{"role": "system", "content": INSIGHT_PROMPT},
                        {"role": "user", "content": insight_prompt(
                            fix.get("kind", "syntax"), fix.get("message", ""), fix.get("word", ""),
                            removed, added)}]
            try:
                text = await self.llm.complete(messages, model=config.LLM_AUDIT_MODEL,
                                               temperature=0.0, json_mode=True, max_tokens=budget)
            except LLMError as exc:
                log.warning("insight call failed: %s", exc)
                return
            obj = parse_json_object(text) or {}
            insight = str(obj.get("insight", "")).strip()
            if not insight or insight.upper().startswith("NONE"):
                continue
            keywords = [str(k) for k in obj.get("keywords", []) or [] if str(k).strip()]
            if fix.get("word"):
                keywords.append(str(fix["word"]))
            try:
                note = self.knowledge.add_insight(insight, source=f"curie:{fix.get('kind', 'syntax')}",
                                                  tables=program_tables(run.source), keywords=keywords)
            except Exception as exc:  # noqa: BLE001
                log.warning("insight save failed: %s", exc)
                return
            if note is not None:
                self.manager.log_llm(session, "insight_saved", {"note": note.id, "text": insight})
                self.manager.notice(session, f"Learned a pitfall ({note.id}): {insight}")

    async def _feed_back(self, session: Session, run: AbapRun, lines: list[str]) -> None:
        shown = lines[:RESULT_MAX_LINES]
        text = "\n".join(shown)
        truncated = len(lines) > len(shown) or len(text) > RESULT_MAX_CHARS
        text = text[:RESULT_MAX_CHARS]
        prompt = result_prompt(run.progname, text.split("\n"), truncated)
        if config.ABAP_INTERPRET_RESULT and lines:
            answer = await self.orchestrator.run_turn(session, prompt, auto=True)
            if answer:
                await self.handle_answer(session, answer)
        else:
            session.messages.append({"role": "user", "content": prompt})
