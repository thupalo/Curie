"""Facts with evidence (knowledge plan 5.2, 7 P2/P3/P6): typed statements
mined from the programs users confirmed, verified against the dictionary
index, kept in the archive, rendered into the notes.

    subject    ZORDERS.CUSTOMER                (table.field)
    predicate  joins | filter_pattern | value_form | key_of
    object     ZCUST.KUNNR | "= space" | "a key part zero padded to 7"
    evidence   [exemplar ids, run ids]
    support    number of distinct programs
    verify     dictionary | none
    status     proposed -> verified (subject and object exist) -> confirmed
               (two independent programs, or a person)

Deterministic: the miner reads program shapes (memory/shape.py); the
verifier checks names against the dictionary index (memory/dictionary.py)
without SAP. The model's part of P2 ("KLIENT means the customer number")
and the SAP probes of P3 are later steps; nothing here invents.

The note synthesiser (P6) drafts a table note for every table that
confirmed programs or tool calls used at least MIN_USES times and that
has no note: the dictionary text, the keys, the fields people used, the
tables read together with it, the facts. Status proposed, for review in
Obsidian; the review job looks at it like any note.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from curie.memory import shape as sh
from curie.memory.store import KnowledgeStore, Note, write_note

log = logging.getLogger(__name__)

MIN_USES = 2      # confirmed programs reading a table before a note is drafted for it
PREDICATES = ("joins", "filter_pattern", "value_form", "key_of")


@dataclass
class Fact:
    subject: str
    predicate: str
    object: str
    evidence: list = field(default_factory=list)
    users: set = field(default_factory=set)
    status: str = "proposed"
    verify: str = "none"
    confidence: float = 0.5
    source: str = "curie:programs"

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.subject}|{self.predicate}|{self.object}".encode("utf-8")).hexdigest()[:12]

    @property
    def support(self) -> int:
        return len(set(self.evidence))

    @property
    def table(self) -> str:
        return self.subject.split(".")[0]

    def as_dict(self) -> dict:
        return {"subject": self.subject, "predicate": self.predicate, "object": self.object,
                "status": self.status, "support": self.support, "verify": self.verify}

    def sentence(self) -> str:
        if self.predicate == "joins":
            return f"{self.subject} joins {self.object}"
        if self.predicate == "filter_pattern":
            return f"{self.subject} is filtered with {self.object}"
        if self.predicate == "value_form":
            return f"{self.subject} takes {self.object}"
        if self.predicate == "key_of":
            return f"{self.subject} is a key of {self.object}"
        return f"{self.subject} {self.predicate} {self.object}"


# --------------------------------------------------------------------------- #
# Mining
# --------------------------------------------------------------------------- #

VALUE_FORMS = {
    ("year", "year"): "a four-character year",
    ("range", "start"): "a date as YYYYMMDD (range start)",
    ("range", "end"): "a date as YYYYMMDD (range end)",
    ("month", "start"): "a date as YYYYMMDD (month start)",
    ("month", "end"): "a date as YYYYMMDD (month end)",
    ("code", "code"): "an upper-case code as written",
    ("code", "prefix"): "an upper-case code with LIKE",
    ("quoted", "prefix"): "a text with LIKE (prefix)",
    ("quoted", "upper"): "a text in upper case",
    ("number", "padded10"): "a number zero padded to 10 characters",
    ("number", "padded7"): "a number zero padded to 7 characters",
}


def mine_program(source: str, question: str = "", evidence: str = "", user: str = "") -> list[Fact]:
    """Facts one confirmed program shows."""
    shape = sh.analyse(source or "")
    if not shape.tables:
        return []
    params = sh.bind(question, shape) if question else []
    param_literals = {p.literal for p in params}
    out: list[Fact] = []

    def add(subject: str, predicate: str, obj: str) -> None:
        f = Fact(subject=subject, predicate=predicate, object=obj)
        if evidence:
            f.evidence.append(evidence)
        if user:
            f.users.add(user)
        out.append(f)

    for join in shape.joins:
        left, right = join.split("=", 1)
        add(left, "joins", right)
    seen_between: set[str] = set()
    for lit in shape.literals:
        if not lit.field or lit.op == "UP TO":
            continue
        key = f"{lit.table}.{lit.field}"
        if lit.raw in param_literals:
            continue                      # a parameter, not a constant of the system
        if lit.op == "BETWEEN":
            if key in seen_between:
                continue
            seen_between.add(key)
            pair = [l for l in shape.literals if l.table == lit.table and l.field == lit.field and l.op == "BETWEEN"]
            if len(pair) >= 2:
                add(key, "filter_pattern", f"BETWEEN {pair[0].raw} AND {pair[1].raw}")
            continue
        if lit.value.isdigit() and len(lit.value) >= 4:
            continue                      # a year, a date, an id: a value, not a convention
        value = "space" if not lit.value else lit.raw
        add(key, "filter_pattern", f"{lit.op} {value}")
    for p in params:
        form = value_form(p.type, p.role, p.example)
        if form and p.field:
            add(p.key, "value_form", form)
    return out


def value_form(vtype: str, role: str, example: str = "") -> str:
    """The wording of a value form; the parts of the configured document
    key (KEY_ROLES) are described from the configuration."""
    vtype = sh.value_type(vtype)
    if vtype == "key":
        width = dict(sh.KEY_ROLES).get(role, 0)
        padded = f" zero padded to {width} characters" if width else ""
        written = f" (the user writes {example})" if example else ""
        return f"the part '{role}' of the document key{padded}{written}"
    return VALUE_FORMS.get((vtype, role), "")


def merge(facts: list[Fact]) -> dict[str, Fact]:
    """The same statement from several programs: one fact, all the evidence."""
    out: dict[str, Fact] = {}
    for f in facts:
        cur = out.get(f.id)
        if cur is None:
            out[f.id] = Fact(subject=f.subject, predicate=f.predicate, object=f.object,
                             evidence=list(f.evidence), users=set(f.users), status=f.status,
                             verify=f.verify, confidence=f.confidence, source=f.source)
        else:
            cur.evidence.extend(e for e in f.evidence if e not in cur.evidence)
            cur.users |= f.users
    return out


def mine(store: KnowledgeStore, archive=None) -> dict[str, Fact]:
    """Facts from the confirmed exemplars and from the archive's programs
    that ran successfully (those without an exemplar)."""
    facts: list[Fact] = []
    for n in store.notes():
        if n.type == "exemplar" and n.status == "confirmed" and n.program:
            facts.extend(mine_program(n.program, n.question, evidence=n.id, user=n.author))
    if archive is not None:
        try:
            rows = archive.query(
                "SELECT p.run_id, p.source, e.question, e.user FROM programs p "
                "JOIN episodes e ON e.id = p.episode_id "
                "WHERE p.run_ok = 1 AND (p.exemplar_id = '' OR p.exemplar_id IS NULL)")
            for r in rows:
                facts.extend(mine_program(r["source"] or "", r["question"] or "",
                                          evidence=f"run:{r['run_id']}", user=r["user"] or ""))
        except Exception as exc:  # noqa: BLE001
            log.debug("facts from the archive: %s", exc)
    return merge(facts)


# --------------------------------------------------------------------------- #
# Verification and promotion
# --------------------------------------------------------------------------- #


def verify(facts: dict[str, Fact], index) -> dict:
    """Subject and object names checked against the dictionary index
    (memory/dictionary.Index); a fact whose names exist becomes verified,
    one whose table is indexed but whose field is not is contradicted.
    Tables the index does not hold stay proposed."""
    counts = {"verified": 0, "contradicted": 0, "unknown": 0}
    tables = getattr(index, "tables", {}) or {}
    for f in facts.values():
        names = [f.subject] + ([f.object] if f.predicate in ("joins", "key_of") else [])
        verdict = "verified"
        for name in names:
            table, _, fld = name.partition(".")
            t = tables.get(table.upper())
            if t is None:
                verdict = "unknown"
                break
            if fld and not any(x.get("n") == fld.upper() for x in t.get("fields", [])):
                verdict = "contradicted"
                break
        counts[verdict] += 1
        if verdict == "verified":
            f.verify = "dictionary"
            if f.status == "proposed":
                f.status = "verified"
        elif verdict == "contradicted":
            f.status = "contradicted"
            f.verify = "dictionary"
    return counts


def promote(facts: dict[str, Fact], min_support: int = 2) -> int:
    """Verified facts that two independent programs show become confirmed."""
    n = 0
    for f in facts.values():
        if f.status == "verified" and f.support >= min_support:
            f.status = "confirmed"
            f.confidence = min(0.95, 0.6 + 0.1 * f.support)
            n += 1
    return n


# --------------------------------------------------------------------------- #
# The archive and the notes
# --------------------------------------------------------------------------- #


def save(archive, facts: dict[str, Fact]) -> int:
    """Upsert into the archive's facts table; a person's status (confirmed
    by hand, retired) is never overwritten by the miner."""
    now = time.time()
    written = 0
    with archive._lock:
        c = archive._conn
        for f in facts.values():
            row = c.execute("SELECT status, source FROM facts WHERE id = ?", (f.id,)).fetchone()
            status = f.status
            if row is not None and row["status"] in ("retired", "confirmed") and \
                    str(row["source"] or "").startswith("user:"):
                status = row["status"]
            c.execute("INSERT INTO facts(id, subject, predicate, object, evidence, support, users, verify, "
                      "status, confidence, created, updated, note_id, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET evidence = excluded.evidence, support = excluded.support, "
                      "users = excluded.users, verify = excluded.verify, status = excluded.status, "
                      "confidence = excluded.confidence, updated = excluded.updated",
                      (f.id, f.subject, f.predicate, f.object, json.dumps(f.evidence), f.support,
                       len(f.users), f.verify, status, f.confidence, now, now, "", f.source))
            written += 1
        # Facts the miner no longer finds (a retired exemplar, a better miner)
        # go, unless a person set their status.
        ids = list(facts)
        marks = ",".join("?" * len(ids)) or "''"
        c.execute(f"DELETE FROM facts WHERE source = 'curie:programs' AND id NOT IN ({marks}) "
                  "AND status NOT IN ('retired') AND NOT (status = 'confirmed' AND source LIKE 'user:%')", ids)
        c.commit()
    return written


def render_into_notes(store: KnowledgeStore, facts: dict[str, Fact]) -> list[str]:
    """Put each table's facts into its table note's `facts:` frontmatter
    (regenerated; the body, which people write, stays)."""
    by_table: dict[str, list[Fact]] = defaultdict(list)
    for f in facts.values():
        if f.status in ("contradicted", "retired"):
            continue
        by_table[f.table].append(f)
    touched = []
    for n in store.notes():
        if n.type != "table":
            continue
        mine_ = sorted(by_table.get(n.name.upper(), []), key=lambda f: (f.predicate, f.subject, f.object))
        new = [f.as_dict() for f in mine_]
        if new != n.facts:
            n.facts = new
            write_note(n)
            touched.append(n.id)
    if touched:
        store.refresh(force=True)
    return touched


def program_usage(store: KnowledgeStore, archive=None) -> dict[str, int]:
    """How many confirmed programs (exemplars, and archived runs without an
    exemplar) read each table: what makes a table worth a note."""
    counts: dict[str, int] = defaultdict(int)
    for n in store.notes():
        if n.type == "exemplar" and n.status == "confirmed":
            for t in n.tables:
                counts[t.upper()] += 1
    if archive is not None:
        try:
            for r in archive.query("SELECT tables FROM programs WHERE run_ok = 1 AND "
                                   "(exemplar_id = '' OR exemplar_id IS NULL)"):
                for t in json.loads(r["tables"] or "[]"):
                    counts[str(t).upper()] += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("program usage from the archive: %s", exc)
    return dict(counts)


def synthesise_notes(store: KnowledgeStore, index, usage: dict[str, int], facts: dict[str, Fact],
                     glossary: dict | None = None, co_read: dict | None = None,
                     min_uses: int = MIN_USES) -> list[Note]:
    """Draft table notes for tables that `usage` (confirmed programs)
    counts at least min_uses times and that have no note (P6)."""
    have = {n.name.upper() for n in store.notes(include_retired=True) if n.type == "table"}
    tables = getattr(index, "tables", {}) or {}
    field_usage = getattr(index, "field_usage", {}) or {}
    by_table: dict[str, list[Fact]] = defaultdict(list)
    for f in facts.values():
        if f.status not in ("contradicted", "retired"):
            by_table[f.table].append(f)
    today = date.today().isoformat()
    out: list[Note] = []
    for name, uses in sorted(usage.items(), key=lambda kv: -kv[1]):
        if uses < min_uses or name in have or name not in tables:
            continue
        t = tables[name]
        fields = t.get("fields", [])
        keys = [f["n"] for f in fields if f.get("k") and f["n"] != "MANDT"]
        used = field_usage.get(name, {})
        top = sorted((f for f in fields if not f.get("k") and used.get(f["n"])), key=lambda f: -used[f["n"]])[:10]
        aliases = []
        for term, entry in (glossary or {}).items():
            if (entry.get("tables") or [""])[0] == name:      # the term's main table only
                aliases.extend(entry.get("forms") or [term])
        partners = []
        if co_read:
            partners = [f"{p} ({c})" for p, c in (co_read.get("tables", {}).get(name) or {}).items()][:4]
        lines = [f"{name}" + (f' ("{t["text"]}")' if t.get("text") else "") + f": {len(fields)} fields, "
                 f"read by {uses} confirmed program(s)."]
        if keys:
            lines.append("Key: " + ", ".join(keys) + ".")
        if top:
            lines.append("Fields programs use: " + "; ".join(
                f"{f['n']}" + (f" ({f['t']})" if f.get("t") else "") for f in top) + ".")
        if partners:
            lines.append("Read together with: " + ", ".join(partners) + ".")
        for f in sorted(by_table.get(name, []), key=lambda f: (f.predicate, f.subject))[:12]:
            lines.append(f"- {f.sentence()} ({f.status}, {f.support} program(s)).")
        lines.append("")
        lines.append("Drafted by Curie from the dictionary and confirmed programs; edit and set "
                     "`status: confirmed` when it is right, or retire it.")
        note = Note(id=f"tables/{name}", path=store.root / "tables" / f"{name}.md")
        note.type = "table"
        note.name = name
        note.tables = [name]
        note.aliases = sorted(set(a for a in aliases if a), key=str.lower)[:8]
        note.keywords = []
        note.status = "proposed"
        note.source = "curie:consolidate"
        note.created = today
        note.body = "\n".join(lines)
        note.facts = [f.as_dict() for f in by_table.get(name, [])]
        write_note(note)
        out.append(note)
    if out:
        store.refresh(force=True)
    return out
