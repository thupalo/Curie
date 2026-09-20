"""The knowledge store: Markdown notes with YAML frontmatter, retrieved by
the SAP names and business terms that appear in a conversation.

Folder layout (KNOWLEDGE_DIR, Obsidian-compatible):

    knowledge/
      tables/ZORDERS.md                what a table holds, its fields' meaning
      concepts/active-agreement.md     a business term and how it maps to data
      hints/20260906-3fa2c1.md         what a user told Curie (/remember)
      exemplars/20260906-A1B2C3D4.md   a confirmed program with its question;
                                       `catalogue: true` makes it a template
      insights/20260909-1a2b3c.md      a one-line rule learned from a failure

Every note:

    ---
    type: table | concept | hint | exemplar | insight
    name: ZORDERS
    aliases: [zamówienie, sales order]
    tables: [ZORDERS]
    keywords: [leasing]
    status: confirmed | proposed | retired
    source: user:THUPALO | doc:<file> | curie:syntax | curie:rules
    created: 2026-09-06
    uses: 0
    successes: 0
    failures: 0
    ---
    The body: what the model should know. [[Wikilinks]] to other notes are
    relationships and count as retrieval keys.

Retrieval is deterministic: table and field names in the text (upper-case
identifiers) match `tables`/`name` exactly; aliases and keywords match by
word prefixes (5 characters), which copes with Polish inflection
("umowy leasingowe" ~ "umów leasingowych"); wikilinks pull neighbours in.
No embeddings. Retired notes are never retrieved.

The three layers of the learning design (CURIE_PLAN.md, FlowEvo):

* exemplars: confirmed programs injected as examples for similar questions;
* insights: pitfalls, one line each, learned from syntax and rule failures
  that were fixed, or written by people;
* the report catalogue: exemplars the user marked correct, replayed for the
  same question with the numbers and quoted values of the new question
  substituted (see find_template / instantiate). Replay still goes through
  the static rules, the syntax check, the audit and the Run button.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

TYPES = ("table", "concept", "hint", "exemplar", "insight", "family")
STATUSES = ("proposed", "confirmed", "retired")
FOLDERS = ("tables", "concepts", "hints", "exemplars", "insights", "families")

IDENT = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{2,29}\b")
WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
FENCE = re.compile(r"```(?:abap|ABAP)?\s*\n(.*?)```", re.DOTALL)
FROM_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+\(?([A-Za-z][A-Za-z0-9_/]{1,29})", re.IGNORECASE)
# Names a program declares itself (internal tables, work areas): never
# database tables, even when they follow FROM (DELETE ADJACENT DUPLICATES
# FROM lt_k, DELETE itab FROM 3).
DECLARED = re.compile(r"^\s*(?:DATA|STATICS|RANGES|CONSTANTS|FIELD-SYMBOLS)?:?\s*"
                      r"(?:BEGIN\s+OF\s+)?<?([A-Za-z_][A-Za-z0-9_]*)>?\s*(?:\(\d+\)\s*)?"
                      r"(?:LIKE|TYPE|OCCURS|VALUE|,|\.)", re.IGNORECASE | re.MULTILINE)
STATEMENT_END = re.compile(r"\.\s*(?:\n|$)")
# Template slots: numbers and quoted values in a question that also occur in
# the program. Identifiers are deliberately not slots (a table name swap
# would change what a reviewed program does).
SLOT = re.compile(r"'[^']*'|\"[^\"]*\"|\d+(?:[.,]\d+)?")
PREFIX = 5


@dataclass
class Note:
    id: str
    path: Path
    type: str = "concept"
    name: str = ""
    aliases: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    links: list = field(default_factory=list)
    status: str = "confirmed"
    source: str = ""
    created: str = ""
    uses: int = 0
    successes: int = 0
    failures: int = 0
    body: str = ""
    question: str = ""     # exemplars
    program: str = ""      # exemplars: the ABAP source from the body's fence
    catalogue: bool = False  # exemplars: replayable template (user marked correct)
    slots: list = field(default_factory=list)   # exemplars: question values found in the program
    audit: dict | None = None                   # exemplars: the verdict at admission
    refinements: list = field(default_factory=list)  # exemplars: follow-up prompts of the thread
    review: dict | None = None                       # the knowledge review's last verdict
    retired_reason: str = ""                         # why status became retired
    superseded_by: str = ""                          # the note that replaced this one
    # Structured extensions (knowledge plan 5.2, 5.3, 5.6); all optional and
    # kept as written, so notes with or without them stay valid:
    facts: list = field(default_factory=list)        # [{subject, predicate, object, ...}]
    family: str = ""                                 # exemplars: the report family id
    params: dict = field(default_factory=dict)       # exemplars/families: {name: {type, value, ...}}
    trigger: str = ""                                # insights: error pattern or syntax word
    counter_evidence: int = 0                        # insights: in the prompt, same error anyway
    mtime: float = 0.0

    @property
    def is_hint(self) -> bool:
        return self.type == "hint"

    @property
    def author(self) -> str:
        return self.source.split(":", 1)[1] if ":" in self.source else self.source

    def frontmatter(self) -> dict:
        fm = {"type": self.type, "name": self.name}
        if self.aliases:
            fm["aliases"] = list(self.aliases)
        if self.tables:
            fm["tables"] = list(self.tables)
        if self.keywords:
            fm["keywords"] = list(self.keywords)
        if self.question:
            fm["question"] = self.question
        if self.type in ("exemplar", "family"):
            fm["catalogue"] = bool(self.catalogue)
            if self.slots:
                fm["slots"] = list(self.slots)
            if self.refinements:
                fm["refinements"] = list(self.refinements)
            if self.audit:
                fm["audit"] = dict(self.audit)
        fm.update({"status": self.status, "source": self.source, "created": self.created,
                   "uses": self.uses, "successes": self.successes, "failures": self.failures})
        if self.review:
            fm["review"] = dict(self.review)
        if self.retired_reason:
            fm["retired_reason"] = self.retired_reason
        if self.superseded_by:
            fm["superseded_by"] = self.superseded_by
        if self.facts:
            fm["facts"] = [dict(f) for f in self.facts]
        if self.family:
            fm["family"] = self.family
        if self.params:
            fm["params"] = dict(self.params)
        if self.trigger:
            fm["trigger"] = self.trigger
        if self.counter_evidence:
            fm["counter_evidence"] = int(self.counter_evidence)
        return fm


# --------------------------------------------------------------------------- #
# Parsing and writing
# --------------------------------------------------------------------------- #


def parse_note(path: Path, root: Path) -> Note:
    text = path.read_text(encoding="utf-8")
    fm: dict = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            try:
                fm = yaml.safe_load(text[3:end]) or {}
            except yaml.YAMLError as exc:
                log.warning("%s: bad frontmatter: %s", path, exc)
                fm = {}
            body = text[end + 4:]
    body = body.strip("\n")
    note = Note(id=path.relative_to(root).with_suffix("").as_posix(), path=path,
                mtime=path.stat().st_mtime)
    note.type = str(fm.get("type", "concept")).lower()
    if note.type not in TYPES:
        note.type = "concept"
    note.name = str(fm.get("name", path.stem))
    note.aliases = _as_list(fm.get("aliases"))
    note.tables = [t.upper() for t in _as_list(fm.get("tables"))]
    if note.type == "table" and note.name.upper() not in note.tables:
        note.tables.insert(0, note.name.upper())
    note.keywords = _as_list(fm.get("keywords"))
    note.status = str(fm.get("status", "confirmed")).lower()
    if note.status not in STATUSES:
        note.status = "confirmed"
    note.source = str(fm.get("source", ""))
    note.created = str(fm.get("created", ""))
    note.uses = int(fm.get("uses", 0) or 0)
    note.successes = int(fm.get("successes", 0) or 0)
    note.failures = int(fm.get("failures", 0) or 0)
    note.question = str(fm.get("question", ""))
    note.catalogue = _truthy(fm.get("catalogue"))
    note.slots = [str(s) for s in _as_list(fm.get("slots"))]
    note.refinements = _as_list(fm.get("refinements")) if isinstance(fm.get("refinements"), list) \
        else ([str(fm["refinements"])] if fm.get("refinements") else [])
    audit = fm.get("audit")
    note.audit = dict(audit) if isinstance(audit, dict) else None
    review = fm.get("review")
    note.review = dict(review) if isinstance(review, dict) else None
    note.retired_reason = str(fm.get("retired_reason") or "")
    note.superseded_by = str(fm.get("superseded_by") or "")
    facts = fm.get("facts")
    note.facts = [dict(f) for f in facts if isinstance(f, dict)] if isinstance(facts, list) else []
    note.family = str(fm.get("family") or "")
    params = fm.get("params")
    note.params = dict(params) if isinstance(params, dict) else {}
    note.trigger = str(fm.get("trigger") or "")
    note.counter_evidence = int(fm.get("counter_evidence", 0) or 0)
    note.links = [m.group(1).strip() for m in WIKILINK.finditer(body)]
    note.body = body
    if note.type in ("exemplar", "family"):
        m = FENCE.search(body)
        note.program = m.group(1).strip("\n") if m else ""
        if not note.tables:
            note.tables = program_tables(note.program)
        if note.type == "family" and not note.question and note.aliases:
            note.question = str(note.aliases[0])
    return note


def write_note(note: Note) -> None:
    fm = yaml.safe_dump(note.frontmatter(), sort_keys=False, allow_unicode=True).strip()
    note.path.parent.mkdir(parents=True, exist_ok=True)
    note.path.write_text(f"---\n{fm}\n---\n{note.body.strip()}\n", encoding="utf-8")
    note.mtime = note.path.stat().st_mtime


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "x", "on")


# --------------------------------------------------------------------------- #
# Matching helpers
# --------------------------------------------------------------------------- #


def words(text: str) -> set[str]:
    return {w.lower() for w in WORD.findall(text or "")}


def identifiers(text: str) -> set[str]:
    """Upper-cased identifier-like tokens: table and field names in prose."""
    return {m.group(0).upper() for m in IDENT.finditer(text or "")}


def prefixes(text: str) -> set[str]:
    return {w[:PREFIX] for w in words(text)}


def phrase_matches(phrase: str, ctx_prefixes: set[str]) -> bool:
    """Every word of the phrase (by 5-char prefix) occurs in the context."""
    ws = [w for w in words(phrase)]
    return bool(ws) and all(w[:PREFIX] in ctx_prefixes for w in ws)


def signature(question: str) -> str:
    """Question reduced to sorted word prefixes; equal for rephrasings."""
    return " ".join(sorted(prefixes(question)))


def declared_names(source: str) -> set[str]:
    """Upper-cased names a program declares (DATA, RANGES, BEGIN OF ...)."""
    out = set()
    for m in DECLARED.finditer(source or ""):
        out.add(m.group(1).upper())
    return out


def _fact_line(f: dict) -> str:
    """One prompt line for a structured fact of a note."""
    subject, predicate, obj = f.get("subject", ""), f.get("predicate", ""), f.get("object", "")
    if predicate == "joins":
        text = f"{subject} joins {obj}"
    elif predicate == "filter_pattern":
        text = f"{subject} is filtered with {obj}"
    elif predicate == "value_form":
        text = f"{subject} takes {obj}"
    else:
        text = f"{subject} {predicate} {obj}"
    status = f.get("status", "")
    tail = f" ({status}, {f.get('support', 0)} program(s))" if status else ""
    return text + tail


def program_tables(source: str) -> list[str]:
    """Database tables a program reads: FROM/JOIN inside SELECT statements,
    minus the names the program declares itself."""
    source = source or ""
    declared = declared_names(source)
    found = set()
    for statement in STATEMENT_END.split(source):
        upper = statement.upper()
        if not upper.lstrip().startswith(("SELECT", "OPEN CURSOR")) and "\nSELECT" not in upper \
                and " SELECT " not in upper:
            continue
        for name in FROM_JOIN.findall(statement):
            name = name.upper()
            if name not in declared and not name.startswith(("LT_", "IT_", "GT_", "T_")):
                found.add(name)
    return sorted(found)


def slot_values(question: str) -> list[str]:
    """Numbers and quoted values of a question, in order of appearance."""
    return [m.group(0) for m in SLOT.finditer(question or "")]


def template_slots(question: str, program: str) -> list[str]:
    """The question's values that occur verbatim in the program."""
    out = []
    for value in slot_values(question):
        bare = value.strip("'\"")
        if bare and bare in program:
            out.append(value)
    return out


def template_signature(question: str) -> str:
    """Question signature with its values abstracted: 'ile umów w 2004' and
    'ile umów w 2005' share it, 'ile umów' does not."""
    return f"{signature(question)} #{len(slot_values(question))}"


def insight_signature(text: str) -> str:
    return " ".join(sorted(prefixes(text)))[:200]


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


class KnowledgeStore:
    def __init__(self, root: str | Path, max_notes: int = 8, max_chars: int = 6000,
                 max_exemplars: int = 2, refresh_every: float = 3.0, max_insights: int = 4):
        self.root = Path(root)
        self.max_notes = max_notes
        self.max_chars = max_chars
        self.max_exemplars = max_exemplars
        self.max_insights = max_insights
        self.refresh_every = refresh_every
        # Optional dictionary check for names that look like tables (set by
        # the runtime to a cached DD02L lookup); None accepts Z/Y names.
        self.table_exists = None
        self._notes: dict[str, Note] = {}
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._signature: tuple = ()
        for sub in FOLDERS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.refresh(force=True)

    # -- loading ---------------------------------------------------------------- #

    def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._loaded_at < self.refresh_every:
            return
        # Notes live in the type folders only. Files at the root (README.md,
        # CARD.md, the EVAL* and REVIEW reports, whatever a person drops
        # there) and dot-folders (.trash, .build, .obsidian) are not notes.
        paths = sorted(p for p in self.root.rglob("*.md")
                       if len(p.relative_to(self.root).parts) > 1
                       and p.relative_to(self.root).parts[0] in FOLDERS
                       and not any(part.startswith(".") for part in p.relative_to(self.root).parts))
        sig = tuple((p.as_posix(), p.stat().st_mtime) for p in paths)
        if not force and sig == self._signature:
            self._loaded_at = now
            return
        notes: dict[str, Note] = {}
        for p in paths:
            try:
                n = parse_note(p, self.root)
                notes[n.id] = n
            except (OSError, UnicodeDecodeError) as exc:
                log.warning("knowledge: cannot read %s: %s", p, exc)
        with self._lock:
            self._notes = notes
            self._signature = sig
            self._loaded_at = now
        log.debug("knowledge: %d notes loaded", len(notes))

    def notes(self, include_retired: bool = False) -> list[Note]:
        self.refresh()
        with self._lock:
            out = list(self._notes.values())
        return out if include_retired else [n for n in out if n.status != "retired"]

    def get(self, note_id: str) -> Note | None:
        self.refresh()
        with self._lock:
            return self._notes.get(note_id)

    def __len__(self) -> int:
        return len(self.notes())

    # -- retrieval ---------------------------------------------------------------- #

    def retrieve(self, text: str, tables: list[str] | None = None) -> tuple[list[Note], list[Note]]:
        """(notes, exemplars) relevant to `text` and the given table names.

        Insights are notes too; at most max_insights of them are chosen."""
        ctx_ids = identifiers(text)
        ctx_ids.update(t.upper() for t in (tables or []))
        ctx_pre = prefixes(text)
        scored: list[tuple[float, Note]] = []
        exemplars: list[tuple[float, Note]] = []
        by_name = {n.name.upper(): n for n in self.notes()}
        for n in self.notes():
            score = 0.0
            if n.name.upper() in ctx_ids:
                score += 3
            score += 3 * sum(1 for t in n.tables if t in ctx_ids)
            score += 2 * sum(1 for a in n.aliases if phrase_matches(a, ctx_pre))
            score += 1 * sum(1 for k in n.keywords if phrase_matches(k, ctx_pre))
            if n.type in ("exemplar", "family"):
                # A family counts as an exemplar: its intents are its aliases
                # (scored above), its template is shown like a program.
                if n.question:
                    q = prefixes(n.question)
                    score += 4 * (len(q & ctx_pre) / max(len(q | ctx_pre), 1))
                if score > 0:
                    exemplars.append((score, n))
                continue
            if n.type == "insight" and not n.tables and not n.keywords and not n.aliases:
                score += 0.25            # a general rule: always a candidate, low priority
            if score > 0:
                scored.append((score, n))
        # Neighbours through wikilinks get a small share of the score.
        matched = {n.id for _, n in scored}
        for _score, n in list(scored):
            for link in n.links:
                target = by_name.get(link.upper())
                if target is not None and target.id not in matched and target.type != "exemplar":
                    scored.append((0.5, target))
                    matched.add(target.id)
        scored.sort(key=lambda s: (-s[0], s[1].type != "table", s[1].status != "confirmed", s[1].id))
        chosen: list[Note] = []
        total = 0
        insights = 0
        for _score, n in scored:
            if len(chosen) >= self.max_notes:
                break
            if n.type == "insight":
                if insights >= self.max_insights:
                    continue
                insights += 1
            if total + len(n.body) > self.max_chars and chosen:
                continue
            chosen.append(n)
            total += len(n.body)
        exemplars.sort(key=lambda s: (-s[0], not s[1].catalogue, s[1].failures, s[1].id))
        return chosen, [n for _s, n in exemplars[: self.max_exemplars]]

    def search(self, query: str, limit: int = 8) -> list[Note]:
        """Free-text search for the sap_knowledge tool: retrieval plus a
        plain word match on names and bodies."""
        notes, exemplars = self.retrieve(query)
        found = list(notes) + list(exemplars)
        seen = {n.id for n in found}
        qp = prefixes(query)
        for n in self.notes():
            if n.id in seen:
                continue
            if qp & prefixes(n.name + " " + n.body):
                found.append(n)
        return found[:limit]

    def co_read(self, tables: list[str], min_count: int = 2) -> list[str]:
        """What confirmed programs that read these tables also read.

        A data insight computed from the exemplars, no model involved:
        "Programs that read ZORDERS also read ZCUST (3 of 4)"."""
        out = []
        exemplars = [n for n in self.notes() if n.type == "exemplar" and n.tables]
        for table in sorted({t.upper() for t in tables}):
            with_table = [n for n in exemplars if table in n.tables]
            if len(with_table) < min_count:
                continue
            counts = Counter(t for n in with_table for t in n.tables if t != table)
            partners = [f"{t} ({c} of {len(with_table)})" for t, c in counts.most_common(3)
                        if c >= min_count and c * 2 >= len(with_table)]
            if partners:
                out.append(f"Confirmed programs that read {table} also read {', '.join(partners)}.")
        return out

    # -- the report catalogue ------------------------------------------------------ #

    def find_template(self, question: str) -> tuple[Note, dict] | None:
        """A catalogued exemplar for the same question shape, and the value
        substitutions {old: new} the new question asks for; None otherwise."""
        question = (question or "").strip()
        if not question:
            return None
        sig = template_signature(question)
        new_values = slot_values(question)
        candidates = [n for n in self.notes() if n.type == "exemplar" and n.catalogue
                      and n.status == "confirmed" and n.program
                      and template_signature(n.question) == sig]
        candidates.sort(key=lambda n: (n.failures, -n.successes, n.id))
        for note in candidates:
            old_values = slot_values(note.question)
            if len(old_values) != len(new_values):
                continue
            slots = set(note.slots or template_slots(note.question, note.program))
            mapping: dict[str, str] = {}
            ok = True
            for old, new in zip(old_values, new_values):
                if old == new:
                    continue
                if old not in slots:
                    ok = False          # a value the program does not carry: not this template
                    break
                mapping[old] = new
            if ok:
                return note, mapping
        return None

    @staticmethod
    def instantiate(note: Note, mapping: dict[str, str]) -> str:
        """The template's program with the old values replaced by the new ones."""
        program = note.program
        for old, new in mapping.items():
            bare_old, bare_new = old.strip("'\""), new.strip("'\"")
            if old.startswith(("'", '"')):
                program = program.replace(f"'{bare_old}'", f"'{bare_new}'")
            else:
                program = re.sub(rf"(?<![\w.]){re.escape(bare_old)}(?![\w.])", bare_new, program)
        return program

    def mark_catalogue(self, note_id: str, on: bool = True) -> Note | None:
        note = self.get(note_id)
        if note is None or note.type not in ("exemplar", "family"):
            return None
        note.catalogue = on
        if on:
            note.status = "confirmed"
            if note.type == "exemplar":
                note.slots = template_slots(note.question, note.program)
        elif note.type == "family":
            note.status = "proposed"      # a family out of the catalogue is not replayed
        write_note(note)
        self.refresh(force=True)
        return note

    def catalogue(self) -> list[Note]:
        return sorted((n for n in self.notes() if n.type in ("exemplar", "family") and n.catalogue),
                      key=lambda n: (n.type != "family", n.id))

    # -- rendering for the prompt -------------------------------------------------- #

    @staticmethod
    def render(notes: list[Note], exemplars: list[Note], extra: list[str] | None = None) -> str:
        parts: list[str] = []
        facts = [n for n in notes if n.type not in ("hint", "insight")]
        hints = [n for n in notes if n.is_hint]
        insights = [n for n in notes if n.type == "insight"]
        if facts:
            parts.append("Facts about this system (curated):")
            for n in facts:
                head = n.name if n.type != "table" else f"table {n.name}"
                text = n.body.strip()
                if n.status == "proposed":
                    head += ", drafted by Curie, not yet reviewed"
                parts.append(f"- [{head}] {text}")
                # Structured facts from confirmed programs (memory/facts.py)
                for f in n.facts[:10]:
                    if f.get("status") in ("contradicted", "retired"):
                        continue
                    parts.append(f"  - {_fact_line(f)}")
        if extra:
            parts.extend(f"- {line}" for line in extra)
        if hints:
            parts.append("Hints users gave earlier (verify against the dictionary when in doubt):")
            for n in hints:
                who = n.author or "a user"
                parts.append(f"- ({who}, {n.created}) {n.body.strip()}")
        if insights:
            parts.append("Pitfalls and rules learned on this system (follow them):")
            for n in insights:
                flag = "" if n.status == "confirmed" else " (unconfirmed)"
                parts.append(f"- {n.body.strip().splitlines()[0]}{flag}")
        if exemplars:
            parts.append("Programs confirmed earlier for similar questions (reuse their "
                         "tables and structure; adapt values):")
            for n in exemplars:
                mark = " [catalogue: marked correct by the user]" if n.catalogue else ""
                if n.type == "family":
                    mark = (" [report family; {{name}} marks a parameter: "
                            + ", ".join(f"{k} = {v.get('type', '?')}" for k, v in n.params.items())
                            + "]") if n.params else " [report family]"
                parts.append(f"Question: {n.question}{mark}\nTables: {', '.join(n.tables) or '-'}\n"
                             f"```abap\n{n.program}\n```")
        return "\n".join(parts)

    # -- writing ---------------------------------------------------------------- #

    def _new_path(self, folder: str, stem: str) -> Path:
        return self.root / folder / f"{date.today().isoformat().replace('-', '')}-{stem}.md"

    def add_hint(self, text: str, user: str, tables: list[str] | None = None,
                 keywords: list[str] | None = None) -> Note:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty hint")
        today = date.today().isoformat()
        note = Note(id="", path=self._new_path("hints", secrets.token_hex(3)))
        note.id = note.path.relative_to(self.root).with_suffix("").as_posix()
        note.type = "hint"
        note.name = text[:60]
        note.tables = sorted({t.upper() for t in (tables or [])} | self._plausible_tables(text))
        note.keywords = list(keywords or [])
        note.status = "confirmed"        # /remember is the user's own confirmation
        note.source = f"user:{user or 'unknown'}"
        note.created = today
        note.body = text
        write_note(note)
        self.refresh(force=True)
        return note

    def add_insight(self, text: str, source: str, tables: list[str] | None = None,
                    keywords: list[str] | None = None, confirmed: bool = False) -> Note | None:
        """Keep a one-line rule; None when an equivalent one exists (its use
        count grows instead)."""
        text = " ".join((text or "").split())
        if len(text) < 12:
            return None
        sig = insight_signature(text)
        for n in self.notes(include_retired=True):
            if n.type == "insight" and insight_signature(n.body.splitlines()[0]) == sig:
                if n.status == "retired":
                    return None          # people retired it; do not learn it again
                n.uses += 1
                write_note(n)
                self.refresh(force=True)
                return None
        today = date.today().isoformat()
        note = Note(id="", path=self._new_path("insights", secrets.token_hex(3)))
        note.id = note.path.relative_to(self.root).with_suffix("").as_posix()
        note.type = "insight"
        note.name = text[:60]
        note.tables = sorted({t.upper() for t in (tables or [])})
        note.keywords = [k for k in (keywords or []) if k][:6]
        note.status = "confirmed" if confirmed else "proposed"
        note.source = source
        note.created = today
        note.body = text
        write_note(note)
        self.refresh(force=True)
        return note

    def add_exemplar(self, question: str, program: str, user: str, run_id: str,
                     tables: list[str] | None = None, audit: dict | None = None,
                     refinements: list[str] | None = None, replace: str = "") -> Note | None:
        """Keep a confirmed program; None when an equivalent one exists.

        `replace` names the exemplar of the same thread: a later accepted
        program of the thread updates that note instead of adding one."""
        question = (question or "").strip()
        program = (program or "").strip("\n")
        if not question or not program:
            return None
        today = date.today().isoformat()
        note = self.get(replace) if replace else None
        if note is not None and note.type == "exemplar":
            note.catalogue = note.catalogue and note.program == program
        else:
            sig = signature(question)
            same = [n for n in self.notes() if n.type == "exemplar" and signature(n.question) == sig]
            if same:
                return None
            note = Note(id="", path=self._new_path("exemplars", run_id))
            note.id = note.path.relative_to(self.root).with_suffix("").as_posix()
            note.type = "exemplar"
            note.source = f"user:{user or 'unknown'}"
            note.created = today
            note.status = "confirmed"
        note.name = question[:60]
        note.question = question
        note.tables = sorted({t.upper() for t in (tables or [])} | set(program_tables(program)))
        note.refinements = [r.strip() for r in (refinements or []) if r.strip()]
        body = f"Confirmed and run successfully by {user} on {today}."
        if note.refinements:
            body += "\n\nRefinements the user asked for on the way:\n" + "\n".join(
                f"- {r}" for r in note.refinements)
        note.body = f"{body}\n\n```abap\n{program}\n```"
        note.program = program
        note.slots = template_slots(question, program)
        if audit:
            note.audit = {k: audit[k] for k in ("allowed", "risk", "summary") if k in audit}
        write_note(note)
        self.refresh(force=True)
        return note

    def retire(self, note_id: str, reason: str = "", superseded_by: str = "") -> Note | None:
        note = self.get(note_id)
        if note is None:
            return None
        note.status = "retired"
        note.catalogue = False
        if reason:
            note.retired_reason = " ".join(reason.split())[:200]
        if superseded_by:
            note.superseded_by = superseded_by
        write_note(note)
        self.refresh(force=True)
        return note

    def record(self, note_ids: list[str], success: bool | None = None) -> None:
        """Count a use and, when known, its outcome; retire exemplars and
        insights that failed twice and more often than they succeeded."""
        for nid in note_ids:
            n = self.get(nid)
            if n is None:
                continue
            n.uses += 1
            if success is True:
                n.successes += 1
                if n.type == "insight" and n.status == "proposed" and n.successes >= 2:
                    n.status = "confirmed"   # it helped twice: promoted
            elif success is False:
                n.failures += 1
                if n.type in ("exemplar", "insight") and n.failures >= 2 and n.failures > n.successes:
                    n.status = "retired"
                    n.catalogue = False
                    n.retired_reason = f"failed {n.failures} times, succeeded {n.successes}"
                    log.info("knowledge: retired %s after %d failures", n.id, n.failures)
            write_note(n)
        if note_ids:
            self.refresh(force=True)

    def _plausible_tables(self, text: str) -> set[str]:
        """Identifiers in a hint that are table names: tables our notes know,
        or Z/Y names the dictionary confirms (when a check is wired), or Z/Y
        names as they are when it is not. Polish words typed in capitals
        (ZAKUPU, ZAWARTY) are the reason for the check."""
        known = {t for n in self.notes() for t in n.tables}
        out = set()
        for ident in identifiers(text):
            if ident in known:
                out.add(ident)
            elif ident[0] in "ZY" and len(ident) >= 4:
                if self.table_exists is None:
                    out.add(ident)
                else:
                    try:
                        if self.table_exists(ident):
                            out.add(ident)
                    except Exception as exc:  # noqa: BLE001 -- a check failure keeps the name out
                        log.debug("table check %s failed: %s", ident, exc)
        return out
