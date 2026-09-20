"""One user turn: stream the model, run its tool calls, hand a final answer
with ABAP to the code path. Owns the session's turn task so that Stop,
window close and heartbeat timeout can cancel it.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets

from curie import config
from curie.llm.client import LLMError
from curie.llm.prompts import system_prompt
from curie.llm.tools import tool_message
from curie.memory.threads import has_data_signal, is_followup, is_general
from curie.sessions.model import Session, State

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, manager, llm, toolbox, knowledge=None):
        self.manager = manager
        self.llm = llm
        self.toolbox = toolbox
        self.knowledge = knowledge  # memory.KnowledgeStore or None
        self.flow = None  # set by cli.py: the AbapFlow

    # -- entry points (loop thread) --------------------------------------------- #

    def start_turn(self, session: Session, text: str) -> bool:
        """Start a user turn as a task; False when one is running or the session is closed."""
        if session.closed or session.busy or session.state is State.RUNNING:
            return False
        if session.turn_task is not None and not session.turn_task.done():
            return False
        text = (text or "").strip()
        if not text:
            return False
        if session.state is State.CONFIRM:
            # A new prompt while code waits: the old code is dropped.
            for run in session.runs.values():
                if run.status == "ready":
                    run.status = "discarded"
        session.turn_task = asyncio.create_task(self._turn(session, text), name=f"turn-{session.id[:8]}")
        return True

    # Chat commands typed into the composer instead of a prompt.
    COMMANDS = ("/reset", "/clear", "/remember", "/hint", "/forget", "/knowledge", "/catalogue")

    def command(self, session: Session, text: str) -> str | None:
        """Handle a slash command; returns its name, or None for a normal prompt."""
        stripped = (text or "").strip()
        word, _, rest = stripped.partition(" ")
        word = word.lower()
        if word not in self.COMMANDS:
            return None
        if word in ("/reset", "/clear"):
            if session.state is State.RUNNING and self.flow is not None:
                self.flow.stop_run(session)      # aborts the job, then the flow goes IDLE
            self.manager.reset(session)
            session.thread = []
            session.thread_exemplar_id = ""
            return "reset"
        if self.knowledge is None:
            self.manager.notice(session, "Knowledge is switched off (KNOWLEDGE_ENABLED=0).", "warn")
            return word[1:]
        if word in ("/remember", "/hint"):
            fact = rest.strip()
            if not fact:
                self.manager.notice(session, f"Usage: {word} <fact worth keeping>", "warn")
                return "remember"
            # Tagged with the tables the hint names and those of the current
            # thread only; tagging with every table of the session made hints
            # fire on every question about the main table.
            note = self.knowledge.add_hint(fact, session.sap.user, sorted(session.thread_tables))
            self.manager.log_llm(session, "remember", {"note": note.id, "text": fact})
            self.manager.notice(session, f"Remembered as {note.id}"
                                + (f" (tables {', '.join(note.tables)})" if note.tables else "")
                                + ". Use /forget " + note.id + " to retire it.")
            session.messages.append({"role": "user", "content": f"[The user asked Curie to remember: {fact}]"})
            return "remember"
        if word == "/forget":
            note = self.knowledge.retire(rest.strip()) if rest.strip() else None
            if note is None:
                self.manager.notice(session, "Usage: /forget <note id>, as shown by /knowledge", "warn")
            else:
                self.manager.log_llm(session, "forget", {"note": note.id})
                self.manager.notice(session, f"Retired {note.id}.")
            return "forget"
        if word == "/catalogue":
            return self._catalogue_command(session, rest.strip())
        # /knowledge: what applies to the current conversation
        notes, exemplars = self.knowledge.retrieve(session.last_prompt or "", sorted(session.tables_used))
        if not notes and not exemplars:
            self.manager.notice(session, f"No notes match this conversation. "
                                         f"{len(self.knowledge)} note(s) in the store.")
            return "knowledge"
        lines = [f"{len(self.knowledge)} note(s) in the store; matching this conversation:"]
        for n in notes + exemplars:
            first = (n.question or n.body).strip().splitlines()[0][:100]
            lines.append(f"- {n.id} [{n.type}, {n.status}, {n.source}] {first}")
        self.manager.notice(session, "\n".join(lines))
        return "knowledge"

    def _catalogue_command(self, session: Session, rest: str) -> str:
        """/catalogue            list the replayable reports
           /catalogue add <id>   make an exemplar a template
           /catalogue drop <id>  take it out again (the exemplar stays)"""
        action, _, note_id = rest.partition(" ")
        action = action.lower()
        if action in ("add", "drop") and note_id.strip():
            note = self.knowledge.mark_catalogue(note_id.strip(), on=(action == "add"))
            if note is None:
                self.manager.notice(session, f"No exemplar {note_id.strip()}; ids are shown by "
                                             "/knowledge.", "warn")
            else:
                self.manager.log_llm(session, "catalogue", {"note": note.id, "on": action == "add"})
                self.manager.notice(session, f"{note.id} {'added to' if action == 'add' else 'removed from'} "
                                             "the report catalogue.")
            return "catalogue"
        entries = self.knowledge.catalogue()
        if not entries:
            self.manager.notice(session, "The report catalogue is empty. Mark a run result as "
                                         "correct, or use /catalogue add <exemplar id>.")
            return "catalogue"
        lines = [f"{len(entries)} catalogued report(s), replayed for the same question shape:"]
        for n in entries:
            if n.type == "family":
                values = " (parameters: " + ", ".join(
                    f"{k}: {v.get('type', '?')}" for k, v in n.params.items()) + ")" if n.params else ""
                lines.append(f"- {n.id} [family, {n.successes} ok / {n.failures} failed] "
                             f"{n.question[:90]}{values}")
                continue
            values = f" (values: {', '.join(n.slots)})" if n.slots else ""
            lines.append(f"- {n.id} [{n.author}, {n.created}, {n.successes} ok / {n.failures} failed] "
                         f"{n.question[:90]}{values}")
        self.manager.notice(session, "\n".join(lines))
        return "catalogue"

    def knowledge_for(self, session: Session, extra: str = "") -> str:
        """Rendered notes for the next model call; records the ids injected."""
        if self.knowledge is None or not config.KNOWLEDGE_ENABLED:
            return ""
        tables = sorted(session.tables_used)
        if hasattr(self.knowledge, "context_for"):
            ctx = self.knowledge.context_for(session.last_prompt, tables, extra)
            note_ids, exemplar_ids, text = ctx.notes, ctx.exemplars, ctx.text
        else:                                   # a bare store (tests, tools)
            notes, exemplars = self.knowledge.retrieve(session.last_prompt + "\n" + extra, tables)
            note_ids, exemplar_ids = [n.id for n in notes], [n.id for n in exemplars]
            text = self.knowledge.render(notes, exemplars, self.knowledge.co_read(tables))
        for nid in note_ids:
            if nid not in session.injected_notes:
                session.injected_notes.append(nid)
            if nid not in session.thread_notes:
                session.thread_notes.append(nid)
        for nid in exemplar_ids:
            if nid not in session.injected_exemplars:
                session.injected_exemplars.append(nid)
            if nid not in session.thread_exemplars:
                session.thread_exemplars.append(nid)
        return text

    @property
    def _table_exists(self):
        """A table-name check for the general-question detector: the
        dictionary index when the bundle has one, else None (every
        identifier counts)."""
        knowledge = self.knowledge
        if knowledge is None or not hasattr(knowledge, "dictionary"):
            return None
        try:
            index = knowledge.dictionary
            if not len(index):
                return None
            tables = index.tables
        except Exception:  # noqa: BLE001
            return None
        return lambda name: name.upper() in tables

    def credit_thread(self, session: Session, ok: bool, how: str) -> str:
        """One outcome per thread (plan F11): the first verdict on the
        thread's answer or run result counts; the notes the answers or the
        programs used are credited or debited, the rest only get a use.
        Returns a sentence for the user, empty when nothing was done."""
        if self.knowledge is None:
            return ""
        tid = session.thread_id or "-"
        if tid in session.thread_outcome:
            return ""
        session.thread_outcome[tid] = "up" if ok else "down"
        ids = list(session.thread_notes) + list(session.thread_exemplars)
        if not ids:
            return ""
        material = list(session.thread_answers) + [r.source for r in session.runs.values()
                                                   if r.thread_id == session.thread_id]
        used = used_notes(self.knowledge, ids, material)
        unused = [i for i in ids if i not in used]
        self.knowledge.record(used, success=ok)
        self.knowledge.record(unused, success=None)
        self.manager.log_llm(session, "thread_outcome", {"thread": tid, "ok": ok, "how": how,
                                                         "used": used, "unused": unused})
        text = f"{len(used)} note(s) {'credited' if ok else 'debited'}"
        if unused:
            text += f", {len(unused)} shown but not used"
        return text

    def feedback(self, session: Session, target: str, ident: str, value: str) -> str:
        """Thumbs up or down: the utility signal of the learning layers.

        On an answer: the thread's outcome; the notes and exemplars the
        thread's answers and programs used are credited or debited, once
        per thread. On a run result: "correct" puts the program into the
        report catalogue, "wrong" retires the exemplar; a replayed template
        is credited or debited."""
        if config.KNOWLEDGE_FEEDBACK == "off":
            return "Feedback is switched off on this installation."
        key = f"{target}:{ident}"
        if key in session.feedback:
            return "Already noted."
        session.feedback[key] = value
        ok = value == "up"
        user = session.sap.user or ""
        counted = config.feedback_counts(user)
        self.manager.log_llm(session, "feedback", {"target": target, "id": ident, "value": value,
                                                   "user": user, "counted": counted})
        if not counted:
            # KNOWLEDGE_FEEDBACK=owners and not an owner: the vote is in the
            # log and the archive for the review, it changes no note and
            # admits nothing to the catalogue. The user's own session
            # still learns that the last answer was wrong.
            if not ok:
                session.messages.append({"role": "user", "content":
                                         "[The user marked the last "
                                         f"{'program result' if target == 'run' else 'answer'} as wrong.]"})
            self.manager.notice(session, "Noted, thanks.")
            return "Noted, thanks."
        if self.knowledge is None:
            return "Noted."
        text = "Noted, thanks."
        try:
            if target == "run":
                run = session.runs.get(ident)
                if run is not None and not run.exemplar_id and not run.origin \
                        and run.status == "done" and ok:
                    run.wants_catalogue = True     # capture still running: honoured when it lands
                    text = "Marked correct: the program will enter the report catalogue once it is kept."
                elif run is not None and run.exemplar_id:
                    if ok:
                        note = self.knowledge.mark_catalogue(run.exemplar_id, on=True)
                        if note is not None:
                            text = (f"Marked correct: {note.id} is now in the report catalogue and "
                                    "is replayed for the same question"
                                    + (f" (values {', '.join(note.slots)} can differ)" if note.slots else "")
                                    + ".")
                    else:
                        self.knowledge.retire(run.exemplar_id)
                        text = f"Marked wrong: {run.exemplar_id} retired, it will not be reused."
                elif run is not None and run.origin and (run.origin.get("catalogue") or run.origin.get("family")):
                    source = run.origin.get("family") or run.origin["catalogue"]
                    self.knowledge.record([source], success=ok)
                    text = ("Noted for the replayed report " + source
                            + (": credited." if ok else ": one failure recorded (two retire it)."))
            else:
                done = self.credit_thread(session, ok, "answer")
                if done:
                    text = f"Noted: {done}."
                elif session.thread_id in session.thread_outcome:
                    text = "Noted; this question's outcome was already counted (one per question)."
            if not ok:
                session.messages.append({"role": "user", "content":
                                         "[The user marked the last "
                                         f"{'program result' if target == 'run' else 'answer'} as wrong.]"})
        except Exception as exc:  # noqa: BLE001 -- feedback must never break the session
            log.warning("feedback failed: %s", exc)
        self.manager.notice(session, text)
        return text

    def stop(self, session: Session) -> bool:
        if session.state is State.RUNNING and self.flow is not None:
            return self.flow.stop_run(session)
        task = session.turn_task
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    def open_thread(self, session: Session, text: str) -> bool:
        """The first thing a user turn does: decide whether the prompt
        continues the thread or starts one, reset what belongs to a turn,
        and show the user's message in the browser. Nothing that can take
        time (the catalogue, the router, the model) runs before this.
        Returns whether the prompt is a follow-up."""
        uid = _id()
        context = "\n".join(list(session.thread) + list(session.thread_answers))
        followup = bool(session.thread) and is_followup(text, context=context)
        if followup:
            session.thread.append(text)
            if session.thread_light and has_data_signal(text, self._table_exists):
                session.thread_light = False          # the thread turned to data
        else:
            session.thread = [text]
            session.thread_exemplar_id = ""
            session.thread_id = uid
            session.thread_tables = set()
            session.thread_notes = []
            session.thread_exemplars = []
            session.thread_answers = []
            session.thread_light = bool(config.LLM_LIGHT_PATH) and is_general(text, self._table_exists)
        self.manager.emit(session, "user_message", {"id": uid, "text": text, "thread": session.thread_id,
                                                    "followup": followup})
        session.last_prompt = text
        session.injected_notes = []
        session.injected_exemplars = []
        session.fixes = []
        return followup

    async def _turn(self, session: Session, text: str) -> None:
        try:
            followup = self.open_thread(session, text)
            if (self.flow is not None and self.knowledge is not None and config.KNOWLEDGE_CATALOGUE
                    and not followup and await self.flow.replay(session, text)):
                return
            answer = await self.run_turn(session, text, announced=True)
            if answer and self.flow is not None:
                await self.flow.handle_answer(session, answer)
        except asyncio.CancelledError:
            pass
        except LLMError as exc:
            hint = ""
            if getattr(self.llm, "is_model_not_found", None) and self.llm.is_model_not_found(exc):
                await self.llm.served_models()       # fills the hint, cached afterwards
                hint = self.llm.hint(exc)
            self.manager.notice(session, f"Model error: {exc}{hint}", "error")
        except Exception as exc:  # noqa: BLE001
            log.exception("turn failed")
            self.manager.notice(session, f"Internal error: {type(exc).__name__}: {exc}", "error")
        finally:
            if session.state not in (State.CONFIRM, State.CLOSED):
                self.manager.set_state(session, State.IDLE, "")

    # -- the loop ------------------------------------------------------------------ #

    async def run_turn(self, session: Session, text: str, auto: bool = False,
                       announced: bool = False) -> str | None:
        """Send `text` as the user message and return the final answer text.

        auto=True marks a message Curie composed itself (syntax feedback, a
        program's output): it goes into the history without a user bubble.
        announced=True says open_thread() already showed the message and
        set the thread up (the normal path through _turn).
        """
        mgr = self.manager
        session.messages.append({"role": "user", "content": text})
        light = False
        if not auto:
            if not announced:
                self.open_thread(session, text)
            light = session.thread_light
        mgr.log_llm(session, "llm_user", {"auto": auto, "text": text, "thread": session.thread_id,
                                          "light": light})
        mgr.set_state(session, State.THINKING, "Asking the model")

        last_answer = ""
        budget = None          # grows once when an answer is cut at max_tokens
        if light:
            budget = config.LLM_LIGHT_MAX_TOKENS
        for _round in range(config.LLM_MAX_TOOL_ROUNDS + 1):
            aid = _id()
            mgr.emit(session, "assistant_start", {"id": aid})
            content, calls, finish = "", [], None
            try:
                if light:
                    # The light path: no tools, the system card only, a
                    # short answer (memory/threads.py is_general).
                    knowledge = str(getattr(self.knowledge, "card", "") or "") \
                        if self.knowledge is not None else ""
                    with_tools = False
                else:
                    knowledge = self.knowledge_for(session, extra=(text if auto else "") + "\n" + last_answer)
                    with_tools = getattr(self.llm, "tools_enabled", True)
                messages = [{"role": "system", "content":
                             system_prompt(session, with_tools=with_tools, knowledge=knowledge, light=light)}]
                messages += trim_history(session.messages, config.LLM_HISTORY_MAX_TOKENS)
                # What the model was given (the recorder needs it: which
                # notes were in the prompt, which model, which settings).
                started_call = asyncio.get_running_loop().time()
                mgr.log_llm(session, "llm_call", {
                    "id": aid, "thread": session.thread_id, "model": getattr(self.llm, "model", ""),
                    "temperature": config.LLM_TEMPERATURE, "light": light,
                    "max_tokens": budget or config.LLM_MAX_OUTPUT_TOKENS,
                    "tool_mode": getattr(self.llm, "tool_mode", "auto" if with_tools else "off"),
                    "tools": len(self.toolbox.schemas) if with_tools else 0,
                    "notes": list(session.injected_notes),
                    "exemplars": list(session.injected_exemplars),
                    "knowledge_chars": len(knowledge), "messages": len(messages),
                    "history_chars": sum(len(str(m.get("content") or "")) for m in messages[1:]),
                })
                usage: dict = {}
                async for kind, payload in self.llm.stream(messages,
                                                           tools=self.toolbox.schemas if with_tools else None,
                                                           max_tokens=budget):
                    if kind == "delta":
                        content += payload
                        mgr.emit(session, "assistant_delta", {"id": aid, "text": payload},
                                 persist=False)
                    elif kind == "tool_calls":
                        calls = payload
                    elif kind == "done":
                        content = payload.get("content") or content
                        finish = payload.get("finish_reason")
                        usage = payload.get("usage") or {}
                mgr.log_llm(session, "llm_done", {
                    "id": aid, "finish": finish, "tool_calls": len(calls), "chars": len(content),
                    "ms": int((asyncio.get_running_loop().time() - started_call) * 1000), **usage})
            except asyncio.CancelledError:
                partial = content.strip()
                mgr.emit(session, "assistant_done", {"id": aid, "text": partial, "interrupted": True})
                if partial:
                    session.messages.append({"role": "assistant",
                                             "content": partial + "\n\n[answer interrupted by the user]"})
                mgr.log_llm(session, "llm_interrupted", {"text": partial})
                raise

            if with_tools and not getattr(self.llm, "tools_enabled", True):
                # The server refused tools during this call: say so once.
                mgr.notice(session, "The model server does not accept tool calls; Curie answers "
                                    "without reading SAP data until it is restarted against a "
                                    "server that does (vLLM: --enable-auto-tool-choice "
                                    "--tool-call-parser).", "warn")
            if finish == "length" and not calls and budget is None and config.LLM_GROW_ON_TRUNCATION \
                    and not light:
                # Cut at max_tokens (reasoning models spend it on hidden
                # thinking): ask once more with twice the budget.
                budget = 2 * config.LLM_MAX_OUTPUT_TOKENS
                mgr.emit(session, "assistant_done", {"id": aid, "text": content.strip(),
                                                     "interrupted": False, "truncated": True})
                mgr.notice(session, f"The answer was cut at {config.LLM_MAX_OUTPUT_TOKENS} tokens; "
                                    f"asking again with {budget}.", "info")
                mgr.log_llm(session, "llm_truncated", {"budget": budget, "text": content[:500]})
                continue

            if not calls:
                content = content.strip()
                session.messages.append({"role": "assistant", "content": content})
                session.thread_answers.append(content)
                mgr.emit(session, "assistant_done", {"id": aid, "text": content, "interrupted": False})
                mgr.log_llm(session, "llm_assistant", {"text": content})
                return content

            # Tool round: the assistant message with its calls, then results.
            session.messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {"id": c["id"] or f"call_{i}", "type": "function",
                     "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                    for i, c in enumerate(calls)
                ],
            })
            mgr.emit(session, "assistant_done", {"id": aid, "text": content.strip(), "interrupted": False})
            last_answer = content[:2000]
            for i, call in enumerate(calls):
                call_id = call["id"] or f"call_{i}"
                args = _args(call["arguments"])
                touched = self.toolbox.tables_in(call["name"], args)
                session.tables_used.update(touched)
                session.thread_tables.update(touched)
                text_ = self.toolbox.describe(call["name"], args)
                mgr.set_state(session, State.TOOL, text_)
                mgr.emit(session, "tool_call", {"id": call_id, "name": call["name"], "args": args})
                started = asyncio.get_running_loop().time()
                ok, result, summary = await self.toolbox.dispatch(call["name"], args)
                ms = int((asyncio.get_running_loop().time() - started) * 1000)
                mgr.emit(session, "tool_result", {"id": call_id, "name": call["name"], "ok": ok,
                                                  "summary": summary[:2000], "ms": ms})
                # The full result goes to the log (capped): the archive
                # needs the structures, rows and values the model saw, not
                # only "17 field(s)".
                mgr.log_llm(session, "tool", {"id": call_id, "turn": aid, "thread": session.thread_id,
                                              "name": call["name"], "args": args, "ok": ok,
                                              "summary": summary[:500], "ms": ms,
                                              "result": _clip(result, config.SESSION_LOG_RESULT_CHARS)})
                session.messages.append({"role": "tool", "tool_call_id": call_id,
                                         "content": tool_message(result)})
            mgr.set_state(session, State.THINKING, "Asking the model")

        mgr.notice(session, f"The model used more than {config.LLM_MAX_TOOL_ROUNDS} "
                            "tool calls in one turn; stopping here.", "warn")
        return None


def trim_history(messages: list[dict], max_tokens: int) -> list[dict]:
    """Keep the most recent messages within a rough token budget (chars/4).

    Never cuts between an assistant tool-call message and its tool results.
    """
    budget = max_tokens * 4
    kept: list[dict] = []
    total = 0
    i = len(messages) - 1
    while i >= 0:
        msg = messages[i]
        group = [msg]
        # A tool result belongs with the assistant message that requested it.
        if msg.get("role") == "tool":
            j = i
            while j >= 0 and messages[j].get("role") == "tool":
                j -= 1
            group = messages[j:i + 1] if j >= 0 else messages[:i + 1]
            i = j
        size = sum(len(str(m.get("content") or "")) + 200 for m in group)
        if kept and total + size > budget:
            break
        kept = group + kept
        total += size
        i -= 1
    return kept


def _id() -> str:
    return secrets.token_hex(6)


def used_notes(knowledge, ids: list[str], material: list[str]) -> list[str]:
    """The notes among `ids` the answers or programs in `material` actually
    used: cited by id, or one of their tables read, or (insights, hints) one
    of their keywords or identifiers present. Unknown ids count as used, so
    an old store is never debited by mistake."""
    text = "\n".join(m for m in material if m)
    upper = text.upper()
    words = set(re.findall(r"[A-Z0-9_/]{3,}", upper))
    used: list[str] = []
    getter = getattr(knowledge, "get", None)
    for nid in ids:
        note = getter(nid) if getter is not None else None
        if note is None or nid in text:
            used.append(nid)
            continue
        if any(t.upper() in words for t in note.tables):
            used.append(nid)
            continue
        if note.type in ("insight", "hint", "concept"):
            keys = [k for k in (note.keywords or []) + [note.name or ""] if len(k) >= 3]
            if any(k.upper() in words for k in keys):
                used.append(nid)
    return used


def _clip(result, limit: int):
    """A tool result for the log: as is when small, otherwise the JSON cut
    at `limit` characters with a marker; never raises."""
    import json
    if limit <= 0:
        return None
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    if len(text) <= limit:
        return result
    return {"_truncated": True, "_chars": len(text), "head": text[:limit]}


def _args(text) -> dict:
    if isinstance(text, dict):
        return text
    import json
    try:
        obj = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}
