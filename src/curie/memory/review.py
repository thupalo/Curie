"""The knowledge review job: check facts, flag doubtful notes, report
similar cases. One model call per note that was not reviewed yet.

What it does for every note (exemplars, hints, insights, concepts, tables):

1. Dictionary check, deterministic, when a SAP connection is available:
   every table the note names must exist (DD02L), and identifiers in the
   body that look like fields must exist in one of those tables (DD03L).
2. Similar cases, deterministic: notes of the same type that share tables
   and question or body words, up to five, handed to the model.
3. The model's review, one JSON verdict per note: ok, fix (with a
   rewritten question for exemplars whose question is a follow-up or a
   pronoun reference), duplicate (of a similar note), retire (wrong,
   contradicted, dangerous). Fail-closed: no JSON means "not reviewed",
   and the note is looked at again next run.

The verdict is written into the note's frontmatter (`review:`) together
with a hash of the content, so a note is reviewed once until it changes;
`--all` reviews everything again. The report goes to knowledge/REVIEW.md
(Obsidian shows it next to the notes) and to the log. With apply=True the
safe actions are taken as well: a rewritten question is applied (the old
one goes to `refinements:` and the note becomes `proposed`), a duplicate
with fewer successes is retired, a note the model wants retired is
retired. Nothing is ever deleted.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import date, datetime
from pathlib import Path

from curie import config
from curie.llm.client import LLMError, parse_json_object
from curie.llm.prompts import REVIEW_PROMPT, review_prompt
from curie.memory.store import (KnowledgeStore, Note, identifiers, prefixes, program_tables,
                                write_note)
from curie.memory.threads import is_followup

log = logging.getLogger(__name__)

VERDICTS = ("ok", "fix", "duplicate", "retire")
# Identifiers in a body that are worth checking against DD03L: upper case,
# at least three characters, not a table the note names, not ABAP noise.
NOISE = {"SELECT", "FROM", "WHERE", "INTO", "TABLE", "WRITE", "DATA", "LIKE", "TYPE", "AND",
         "THE", "FOR", "NOT", "WITH", "ROWS", "SINGLE", "COUNT", "SUM", "MAX", "MIN", "AVG",
         "STATUS", "SAP", "RFC", "ABAP", "CHAR", "NUMC", "INT", "DEC", "DATS", "TIMS", "TRUE",
         "FALSE", "NULL", "NONE", "CURIE", "REPORT", "ENDSELECT", "LOOP", "ENDLOOP", "APPEND"}
# Codes of this installation that are not fields (VALUE_STOP_CODES) join
# the noise at check time.


def content_hash(note: Note) -> str:
    text = f"{note.type}|{note.question}|{note.body}|{note.status}"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def similar_notes(store: KnowledgeStore, note: Note, limit: int = 5) -> list[Note]:
    """Same type, shared tables and words; the deterministic 'similar cases'."""
    own_tables = set(note.tables)
    own_words = prefixes(note.question or note.name) | prefixes(note.body[:400])
    scored = []
    for other in store.notes(include_retired=False):
        if other.id == note.id or other.type != note.type:
            continue
        score = 2 * len(own_tables & set(other.tables))
        other_words = prefixes(other.question or other.name) | prefixes(other.body[:400])
        overlap = len(own_words & other_words) / max(len(own_words | other_words), 1)
        score += 6 * overlap
        if score >= 2:
            scored.append((score, other))
    scored.sort(key=lambda s: (-s[0], s[1].id))
    return [n for _s, n in scored[:limit]]


class DictionaryCheck:
    """Table and field existence through RFC_READ_TABLE on DD02L / DD03L, cached per run."""

    def __init__(self, sap):
        self.sap = sap
        self._tables: dict[str, bool] = {}
        self._fields: dict[str, set[str]] = {}

    async def table_exists(self, table: str) -> bool:
        if table not in self._tables:
            from curie.sap_tools.read_table import read_rows
            cols, rows = await self.sap.run(read_rows, self.sap, "DD02L", ["TABNAME"],
                                            f"TABNAME = '{table}'", 1)
            self._tables[table] = bool(rows)
        return self._tables[table]

    async def fields_of(self, table: str) -> set[str]:
        if table not in self._fields:
            from curie.sap_tools.read_table import read_rows
            cols, rows = await self.sap.run(read_rows, self.sap, "DD03L", ["FIELDNAME"],
                                            f"TABNAME = '{table}'", 500)
            self._fields[table] = {str(r[0]).strip().upper() for r in rows}
        return self._fields[table]

    async def check(self, note: Note) -> list[str]:
        problems: list[str] = []
        existing: list[str] = []
        for table in note.tables:
            try:
                ok = await self.table_exists(table)
            except Exception as exc:  # noqa: BLE001 -- a dictionary hiccup is not a finding
                log.warning("dictionary check %s: %s", table, exc)
                return problems
            if ok:
                existing.append(table)
            else:
                problems.append(f"table {table} does not exist in the dictionary")
        if not existing:
            return problems
        noise = NOISE | set(config.VALUE_STOP_CODES)
        candidates = {i for i in identifiers(note.body) if i not in noise and i not in note.tables
                      and len(i) >= 3 and i.isupper() and not i[0].isdigit()}
        # Only names that occur with a table reference nearby are treated as fields:
        # "STATUS in ZORDERS", "ZORDERS-STATUS", "field STATUS".
        fieldish = {c for c in candidates
                    if re.search(rf"(?:-|\bfield\b\s+|\bpole\b\s+|\bkolumn\w*\s+){c}\b|\b{c}\s+(?:in|w|of)\s+[A-Z]",
                                 note.body)}
        if not fieldish:
            return problems
        known: set[str] = set()
        for table in existing:
            try:
                known |= await self.fields_of(table)
            except Exception as exc:  # noqa: BLE001
                log.warning("dictionary check %s fields: %s", table, exc)
                return problems
        for name in sorted(fieldish - known):
            problems.append(f"field {name} is not in {', '.join(existing)}")
        return problems


class KnowledgeReviewer:
    def __init__(self, store: KnowledgeStore, llm, sap=None):
        self.store = store
        self.llm = llm
        self.sap = sap
        self.last: dict | None = None
        self._running = False

    # -- selection ------------------------------------------------------------------ #

    def pending(self, everything: bool = False) -> list[Note]:
        out = []
        for note in self.store.notes(include_retired=False):
            review = note.review or {}
            if everything or review.get("hash") != content_hash(note):
                out.append(note)
        out.sort(key=lambda n: (n.type != "exemplar", n.id))
        return out[: config.KNOWLEDGE_REVIEW_MAX]

    def outstanding(self) -> list[Note]:
        """Notes with a stored verdict other than ok that was not applied and
        that still describes the note (same content hash): what an earlier
        run flagged and nobody acted on yet."""
        out = []
        for note in self.store.notes(include_retired=False):
            review = note.review or {}
            if review.get("verdict", "ok") == "ok" or review.get("applied"):
                continue
            if review.get("hash") != content_hash(note):
                continue
            out.append(note)
        out.sort(key=lambda n: (n.type != "exemplar", n.id))
        return out

    # -- one note ------------------------------------------------------------------- #

    async def review_note(self, note: Note, ddic: DictionaryCheck | None) -> dict | None:
        """The verdict dict, or None when the model gave none (fail-closed)."""
        facts = await ddic.check(note) if ddic is not None else []
        similar = similar_notes(self.store, note)
        hints = []
        if note.type == "exemplar" and note.question and is_followup(note.question):
            hints.append("the question reads like a follow-up on an earlier prompt")
        if note.type == "exemplar" and re.search(r"\b(tej|tego|tych|ta|ten|this|that|these)\b",
                                                 note.question, re.IGNORECASE):
            hints.append("the question refers to something outside itself (this/that)")
        messages = [{"role": "system", "content": REVIEW_PROMPT},
                    {"role": "user", "content": review_prompt(note, similar, facts, hints)}]
        try:
            text = await self.llm.complete(messages, model=config.KNOWLEDGE_REVIEW_MODEL,
                                           temperature=0.0, json_mode=True,
                                           max_tokens=max(int(config.LLM_MAX_OUTPUT_TOKENS), 1024))
        except LLMError as exc:
            log.warning("review of %s failed: %s", note.id, exc)
            return None
        obj = parse_json_object(text)
        if not obj:
            log.warning("review of %s: no JSON verdict: %r", note.id, (text or "")[:120])
            return None
        verdict = str(obj.get("verdict", "")).lower().strip()
        if verdict not in VERDICTS:
            return None
        problems = [str(p).strip() for p in (obj.get("problems") or []) if str(p).strip()][:5]
        problems = facts + problems
        duplicate_of = str(obj.get("duplicate_of") or "").strip()
        if verdict == "duplicate" and duplicate_of not in {n.id for n in similar}:
            verdict, duplicate_of = ("fix" if problems else "ok"), ""
        question = " ".join(str(obj.get("question") or "").split())
        if question.lower() in ("null", "none") or len(question) < 12 or note.type != "exemplar":
            question = ""
        if facts and verdict == "ok":
            verdict = "fix"
        return {"date": date.today().isoformat(), "hash": content_hash(note), "verdict": verdict,
                "problems": problems, "question": question or None,
                "duplicate_of": duplicate_of or None,
                "similar": [n.id for n in similar][:3] or None,
                "confidence": _confidence(obj.get("confidence"))}

    # -- the run ---------------------------------------------------------------------- #

    async def run(self, apply: bool = False, everything: bool = False) -> dict:
        if self._running:
            return {"skipped": "a review is already running"}
        self._running = True
        started = datetime.now()
        report = {"started": started.isoformat(timespec="seconds"), "reviewed": 0,
                  "unreviewed": 0, "applied": 0, "flags": [], "dictionary": bool(self.sap)}
        ddic = DictionaryCheck(self.sap) if self.sap is not None else None
        try:
            seen: set[str] = set()
            for note in self.pending(everything):
                verdict = await self.review_note(note, ddic)
                fresh = self.store.get(note.id) or note
                if verdict is None:
                    report["unreviewed"] += 1
                    continue
                fresh.review = {k: v for k, v in verdict.items() if v not in (None, [], "")}
                report["reviewed"] += 1
                seen.add(fresh.id)
                if verdict["verdict"] != "ok":
                    if apply and self._apply(fresh, verdict):
                        report["applied"] += 1
                        fresh.review["applied"] = date.today().isoformat()
                    report["flags"].append({"id": fresh.id, "type": fresh.type, **fresh.review})
                write_note(fresh)
                await asyncio.sleep(0)
            # Verdicts of earlier runs that nobody acted on stay in the report
            # (and are applied now with --apply): a report is never a one-off.
            report["outstanding"] = 0
            for note in self.outstanding():
                if note.id in seen:
                    continue
                verdict = dict(note.review or {})
                if apply and self._apply(note, verdict):
                    report["applied"] += 1
                    note.review["applied"] = date.today().isoformat()
                    write_note(note)
                else:
                    report["outstanding"] += 1
                report["flags"].append({"id": note.id, "type": note.type, "earlier": True, **note.review})
            self.store.refresh(force=True)
            report["finished"] = datetime.now().isoformat(timespec="seconds")
            self.write_report(report)
            self.last = report
            log.info("knowledge review: %d reviewed, %d flagged, %d applied, %d without verdict",
                     report["reviewed"], len(report["flags"]), report["applied"], report["unreviewed"])
            return report
        finally:
            self._running = False

    def _apply(self, note: Note, verdict: dict) -> bool:
        kind = verdict["verdict"]
        if kind == "fix" and not verdict.get("question"):
            # "table LT_K does not exist": a name the old table detection
            # picked up from the program; drop it when the program does not
            # read it. Other problems need a person.
            bad = [m.group(1).upper() for p in verdict.get("problems") or []
                   for m in [re.match(r"table (\S+) does not exist", p, re.IGNORECASE)] if m]
            if bad and note.type in ("exemplar", "hint", "insight", "family"):
                keep = set(program_tables(note.program)) if note.program else set()
                before = list(note.tables)
                note.tables = [t for t in note.tables if t not in bad or t in keep]
                return note.tables != before
            return False
        if kind == "fix" and verdict.get("question"):
            old = note.question
            if " ".join(old.split()) == " ".join(verdict["question"].split()):
                return False              # nothing to change
            if any(str(r).startswith("was: ") for r in note.refinements):
                return False              # rewritten once already: a person decides the rest
            note.refinements = [r for r in note.refinements if r != old] + [f"was: {old}"]
            note.question = verdict["question"]
            note.name = note.question[:60]
            note.status = "proposed"
            note.catalogue = False
            return True
        if kind == "retire":
            note.status = "retired"
            note.catalogue = False
            note.body = f"Retired by the knowledge review on {verdict['date']}: " \
                        f"{'; '.join(verdict['problems']) or 'see review'}.\n\n{note.body}"
            return True
        if kind == "duplicate" and verdict.get("duplicate_of"):
            other = self.store.get(verdict["duplicate_of"])
            if other is None or other.status == "retired":
                return False
            loser = note if (note.successes, -note.failures) <= (other.successes, -other.failures) else other
            loser.status = "retired"
            loser.catalogue = False
            loser.body = f"Retired by the knowledge review on {verdict['date']} as a duplicate of " \
                         f"{other.id if loser is note else note.id}.\n\n{loser.body}"
            if loser is not note:
                write_note(loser)
            return True
        return False

    # -- report ------------------------------------------------------------------------ #

    def write_report(self, report: dict) -> Path:
        path = self.store.root / "REVIEW.md"
        lines = [f"# Knowledge review, {report['started'][:16].replace('T', ' ')}", "",
                 f"Reviewed {report['reviewed']} note(s), flagged {len(report['flags'])} "
                 f"({report.get('outstanding', 0)} from earlier runs, still open), "
                 f"applied {report['applied']} change(s), {report['unreviewed']} without a verdict. "
                 f"Dictionary check: {'on' if report.get('dictionary') else 'off (no SAP connection)'}.",
                 ""]
        if not report["flags"]:
            lines.append("Nothing to flag.")
        for flag in report["flags"]:
            state = " (applied)" if flag.get("applied") else (" (from an earlier run)" if flag.get("earlier") else "")
            lines.append(f"## [[{flag['id']}]] ({flag['type']}): {flag['verdict']}{state}")
            for p in flag.get("problems") or []:
                lines.append(f"- {p}")
            if flag.get("question"):
                lines.append(f"- suggested question: {flag['question']}")
            if flag.get("duplicate_of"):
                lines.append(f"- duplicate of [[{flag['duplicate_of']}]]")
            if flag.get("similar"):
                lines.append("- similar: " + ", ".join(f"[[{s}]]" for s in flag["similar"]))
            lines.append("")
        lines.append("Verdicts are also in each note's `review:` frontmatter and stay listed here "
                     "until applied or the note changes. `curie --review-knowledge --apply` applies "
                     "question rewrites, retirements and table corrections, also for earlier runs; "
                     "problems without a rewrite need a person (edit the note, or /forget it).")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def _confidence(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(min(max(f, 0.0), 1.0), 2)
