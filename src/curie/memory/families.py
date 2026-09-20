"""Report families: a confirmed program with typed holes (knowledge plan
5.3, 7 P4, 8).

A family is built from confirmed exemplars that share a shape
(memory/shape.py): the canonical member's program with its parameter
literals replaced by {{name}}, the parameters typed by the values of the
canonical question (agreement, year, month, period, quoted, code,
number), the member questions with their values masked as the family's
intents. It is a note in knowledge/families/:

    ---
    type: family
    name: zorders-count-gjahr
    intents: ["Ile umów leasingowych zostało zawartych w {year}?", ...]
    tables: [ZORDERS]
    params: {gjahr: {type: year, role: year, field: ZORDERS.GJAHR, op: "=", literal: "'2005'"}}
    members: [exemplars/20260907-42BBA1AF, exemplars/20260907-9F19ABA5]
    canonical: exemplars/20260907-9F19ABA5
    shape: 3f2a...
    status: confirmed | proposed
    ---
    One line of description (the model writes the PL and EN intent, the
    builder a fallback).

    ```abap
    ... the template with {{gjahr}} ...
    ```

Admission (plan D5): a family whose canonical member is in the report
catalogue is `confirmed` and replayable; otherwise it is `proposed`: shown
as the exemplar for a matching question, never replayed until a person
confirms it (edit the status, or `/catalogue add <family id>`).

Matching a question (plan 8): its typed values must fill every parameter,
and its masked wording must be close to one of the family's intents
(word-prefix Jaccard, MATCH_THRESHOLD) or share the family's exact
template signature. Identifiers never become parameters, so a template
can never be steered to another table.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from curie.memory import shape as sh
from curie.memory.store import KnowledgeStore, Note, prefixes, template_signature, write_note

log = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.5      # masked question vs a family intent (word-prefix Jaccard)
MAX_EXTRA_WORDS = 2        # content words of the question absent from the closest intent: more
                           # of them means conditions the family does not have ("marki Ford")
VARIANT_THRESHOLD = 0.75   # program skeletons of two members (difflib ratio)
FOLDER = "families"


@dataclass
class FamilyMatch:
    note: Note
    program: str
    values: dict = field(default_factory=dict)   # param name -> rendered value
    score: float = 0.0
    kind: str = "family"                         # "family" | "catalogue"
    routed: bool = False                         # picked by the model (route), not lexically

    @property
    def description(self) -> str:
        vals = ", ".join(f"{k} = {v}" for k, v in self.values.items()) or "the same values"
        return f"{self.note.id} with {vals}"


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #


@dataclass
class Draft:
    id: str
    shape: sh.Shape
    canonical: Note
    members: list
    params: list
    template: str
    intents: list

    def as_note(self, store: KnowledgeStore, existing: Note | None = None) -> Note:
        today = date.today().isoformat()
        note = existing or Note(id=f"{FOLDER}/{self.id}", path=store.root / FOLDER / f"{self.id}.md")
        note.type = "family"
        note.name = self.id
        note.tables = list(self.shape.tables)
        note.aliases = list(self.intents)
        note.params = {p.name: p.as_dict() for p in self.params}
        note.family = self.canonical.id
        note.source = note.source or "curie:consolidate"
        note.created = note.created or today
        confirmed = self.canonical.catalogue or any(m.catalogue for m in self.members)
        if existing is None or existing.status == "proposed":
            note.status = "confirmed" if confirmed else "proposed"
        note.catalogue = note.status == "confirmed"
        members = [m.id for m in self.members]
        body_head = note.body.split("\n\n```abap", 1)[0].strip() if existing is not None and \
            existing.body and not existing.body.startswith("Report family") else ""
        description = body_head or (
            f"Report family built from {len(members)} confirmed program(s): {self.shape.describe()}. "
            f"Parameters: " + (", ".join(f"{p.name} ({p.type})" for p in self.params) or "none") + ".")
        note.body = (f"{description}\n\nMembers: " + ", ".join(f"[[{m}]]" for m in members)
                     + f"\nCanonical: [[{self.canonical.id}]]\nShape: {self.shape.signature()}"
                     + f"\n\n```abap\n{self.template}\n```")
        note.program = self.template
        note.question = self.intents[0] if self.intents else ""
        note.slots = []
        return note


def family_id(shape: sh.Shape, params: list) -> str:
    tables = "-".join(t.lower() for t in shape.tables[:2]) or "report"
    agg = (shape.aggregates[0].lower() if shape.aggregates else "list")
    keys = "-".join(sorted({p.name for p in params})) or "fixed"
    return re.sub(r"[^a-z0-9\-]+", "", f"{tables}-{agg}-{keys}")[:60]


def build(store: KnowledgeStore, min_members: int = 2) -> list[Draft]:
    """Cluster confirmed exemplars by shape; a cluster with min_members
    members, or with a catalogue member, becomes a family draft."""
    groups: dict[str, list[tuple[Note, sh.Shape]]] = {}
    for n in store.notes():
        if n.type != "exemplar" or n.status != "confirmed" or not n.program or not n.question:
            continue
        shape = sh.analyse(n.program)
        if not shape.tables:
            continue
        groups.setdefault(shape.signature(), []).append((n, shape))
    drafts: list[Draft] = []
    for sig, members in groups.items():
        catalogued = [m for m in members if m[0].catalogue]
        if len(members) < min_members and not catalogued:
            continue
        # canonical: catalogue member, else most successes, else newest
        members.sort(key=lambda m: (not m[0].catalogue, -m[0].successes, m[0].failures, m[0].id))
        canonical, shape = members[0]
        params = sh.bind(canonical.question, shape)
        template = sh.templatize(canonical.program, params)
        spec = {p.name: p.as_dict() for p in params}
        # Members whose question fills the parameters are variants; others
        # share the shape but not the intent (a different question on the
        # same tables) and are left out.
        kept: list[Note] = []
        intents: list[str] = []
        canon_skeleton = sh.skeleton(canonical.program, params)
        for note, member_shape in members:
            if note is not canonical:
                if sh.fill(template, spec, sh.question_values(note.question)) is None:
                    continue
                # Same shape is not enough: the logic must be the same too
                # (a top-customer report and a per-region report can share
                # every table and filter).
                own = sh.skeleton(note.program, sh.bind(note.question, member_shape))
                if sh.similarity(canon_skeleton, own) < VARIANT_THRESHOLD:
                    continue
            kept.append(note)
            masked = sh.masked_question(note.question)
            if masked not in intents:
                intents.append(masked)
        if len(kept) < min_members and not canonical.catalogue:
            continue
        drafts.append(Draft(id=family_id(shape, params), shape=shape, canonical=canonical,
                            members=kept, params=params, template=template, intents=intents))
    return drafts


def write(store: KnowledgeStore, drafts: list[Draft]) -> list[Note]:
    """Write or update the family notes; a note a person confirmed keeps
    its description, intents and status."""
    out = []
    (store.root / FOLDER).mkdir(parents=True, exist_ok=True)
    for d in drafts:
        existing = store.get(f"{FOLDER}/{d.id}")
        if existing is not None and existing.status == "retired":
            continue
        prior = list(existing.aliases) if existing is not None and existing.status == "confirmed" else []
        note = d.as_note(store, existing)
        if prior:                         # intents a person added stay
            note.aliases = list(d.intents) + [a for a in prior if a not in d.intents]
        write_note(note)
        out.append(note)
    # A family whose canonical member is no longer a confirmed exemplar
    # (retired, or set back to proposed by a review rewrite) is not
    # replayed any more: it goes back to proposed until a person decides.
    drafted = {n.id for n in out}
    for fam in store.notes():
        if fam.type != "family" or fam.id in drafted or fam.status != "confirmed":
            continue
        canonical = store.get(fam.family) if fam.family else None
        if canonical is None or canonical.status != "confirmed":
            fam.status = "proposed"
            fam.catalogue = False
            fam.retired_reason = ""
            fam.body = (f"Set back to proposed: its canonical program {fam.family or '?'} is "
                        f"{'gone' if canonical is None else canonical.status}.\n\n" + fam.body)
            write_note(fam)
            log.info("family %s set back to proposed: canonical %s is %s", fam.id, fam.family,
                     "gone" if canonical is None else canonical.status)
    store.refresh(force=True)
    return out


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


def rebuild_template(store: KnowledgeStore, note: Note) -> str | None:
    """The family's template made again from its canonical member (the
    current templatize rules); None when the member is gone."""
    canonical = store.get(note.family) if note.family else None
    if canonical is None or not canonical.program or not canonical.question:
        return None
    shape = sh.analyse(canonical.program)
    params = sh.bind(canonical.question, shape)
    if {p.name for p in params} != set(note.params):
        return None
    return sh.templatize(canonical.program, params)


def fill_checked(store: KnowledgeStore, note: Note, values: list) -> tuple[str, dict] | None:
    """Fill the family's template and make sure no value of the canonical
    question survives inside its texts. A template that leaks (built
    before the written form of a value became a hole) is rebuilt from
    the canonical member and saved; when that still leaks, the family is
    not replayed."""
    filled = sh.fill(note.program, note.params, values)
    if filled is None:
        return None
    leaks = sh.leaked_values(note.params, filled[0], values)
    if not leaks:
        return filled
    template = rebuild_template(store, note)
    if template and template != note.program:
        again = sh.fill(template, note.params, values)
        if again is not None and not sh.leaked_values(note.params, again[0], values):
            note.program = template
            note.body = re.sub(r"```abap\n.*?```", "```abap\n" + template + "\n```", note.body, count=1,
                               flags=re.DOTALL)
            write_note(note)
            log.info("family %s: template rebuilt, it printed %s from its canonical question",
                     note.id, ", ".join(leaks))
            return again
    log.warning("family %s not replayed: its template prints %s from its canonical question",
                note.id, ", ".join(leaks))
    return None


def match(store: KnowledgeStore, question: str, replayable_only: bool = True) -> FamilyMatch | None:
    """The best family for a question, with its template filled."""
    question = (question or "").strip()
    if not question:
        return None
    values = sh.question_values(question)
    masked = sh.masked_question(question)
    q_pre = prefixes(masked)
    q_sig = template_signature(question)
    best: FamilyMatch | None = None
    for n in store.notes():
        if n.type != "family" or not n.program or not n.params and not n.aliases:
            continue
        if replayable_only and n.status != "confirmed":
            continue
        filled = fill_checked(store, n, values)
        if filled is None:
            continue
        score = 0.0
        extra = len(q_pre)
        for intent in n.aliases:
            if template_signature(intent.replace("{", "").replace("}", "")) == q_sig:
                score = max(score, 1.0)
                extra = 0
            ip = prefixes(intent)
            if ip:
                score = max(score, len(ip & q_pre) / len(ip | q_pre))
                extra = min(extra, len(q_pre - ip))
        if score < MATCH_THRESHOLD or extra > MAX_EXTRA_WORDS:
            # Words the family never saw are conditions it cannot apply
            # ("na pojazdy marki Ford z województwa śląskiego"): the
            # question goes to the model, with the family as its exemplar.
            continue
        if best is None or score > best.score or (score == best.score and n.successes > best.note.successes):
            best = FamilyMatch(note=n, program=filled[0], values=filled[1], score=round(score, 3))
    return best


# --------------------------------------------------------------------------- #
# The model as a router (plan 8.3)
# --------------------------------------------------------------------------- #

ROUTER_PROMPT = """\
You route a user's question about an SAP system to one of the known report
families, or to none. A family is a reviewed program with typed parameters;
it fits only when the question asks for the same report with other
parameter values, not for a different report, another table, other columns
or another aggregation. Return a JSON object only:

{"family": "<family id>" or null, "values": {"<parameter>": "<value exactly as written in the question>"}}

Rules: pick a family only when every parameter has a value in the question;
copy each value verbatim from the question; when the question adds a
condition, a column or a grouping the family does not have, or when unsure,
return {"family": null}.
"""


def candidates(store: KnowledgeStore, question: str, tables: list[str] | None = None,
               replayable_only: bool = True) -> list[Note]:
    """Families with a lexical signal for the question (a shared table, or
    any word in common with an intent) whose parameters the question's
    values could fill: the only ones the router may pick from."""
    values = sh.question_values(question)
    types = {v.type for v in values} | ({"range"} if any(v.is_range for v in values) else set())
    placeholder = re.compile(r"\{\w+\}")
    q_pre = prefixes(placeholder.sub(" ", sh.masked_question(question)))
    q_tables = {t.upper() for t in (tables or [])} | {w for w in re.findall(r"\b[A-Z][A-Z0-9_]{3,}\b",
                                                                              question.upper())}
    out: list[Note] = []
    for n in store.notes():
        if n.type != "family" or not n.program or not n.params:
            continue
        if replayable_only and n.status != "confirmed":
            continue
        if any(sh.value_type(spec.get("type", "")) not in types for spec in n.params.values()):
            continue
        shared_table = bool(set(n.tables) & q_tables)
        shared_words = any(prefixes(placeholder.sub(" ", intent)) & q_pre for intent in n.aliases)
        if shared_table or shared_words:
            out.append(n)
    return out


def router_prompt(question: str, notes: list[Note]) -> str:
    lines = [f"Question: {question}", "", "Families:"]
    for n in notes:
        params = ", ".join(f"{name} ({spec.get('type', '?')}, e.g. {spec.get('example', '')})"
                           for name, spec in n.params.items())
        intents = " | ".join(n.aliases[:4])
        lines.append(f"- id: {n.id}\n  asks: {intents}\n  tables: {', '.join(n.tables)}\n  parameters: {params}")
    return "\n".join(lines)


def routed(note: Note, values_text: dict, question: str) -> FamilyMatch | None:
    """The model's pick, admitted only when every value it named is in the
    question and fills the template through the typed extractor; None
    otherwise (fail-closed)."""
    if not note.params or not isinstance(values_text, dict):
        return None
    qvals = sh.question_values(question)
    q_norm = " ".join(question.split()).lower()
    ordered: list[sh.Value] = []
    for name in note.params:
        raw = " ".join(str(values_text.get(name, "")).split())
        if not raw or raw.lower() not in q_norm:
            return None
        chosen = None
        for v in qvals:
            vt = " ".join(v.text.split()).lower()
            if vt == raw.lower() or raw.lower() in vt or vt in raw.lower():
                chosen = v
                break
        if chosen is None:
            return None
        if chosen not in ordered:
            ordered.append(chosen)
    filled = sh.fill(note.program, note.params, ordered)
    if filled is None or sh.leaked_values(note.params, filled[0], ordered):
        return None
    return FamilyMatch(note=note, program=filled[0], values=filled[1], score=0.0, routed=True)


async def route(store: KnowledgeStore, question: str, llm, model: str | None = None,
                tables: list[str] | None = None, replayable_only: bool = True) -> FamilyMatch | None:
    """Ask the model to pick a family among the lexical candidates; None
    when there are no candidates, the model declines, or its pick does not
    validate."""
    from curie import config
    from curie.llm.client import LLMError, parse_json_object

    notes = candidates(store, question, tables, replayable_only)
    if not notes:
        return None
    try:
        text = await llm.complete(
            [{"role": "system", "content": ROUTER_PROMPT},
             {"role": "user", "content": router_prompt(question, notes)}],
            model=model or config.KNOWLEDGE_ROUTER_MODEL, temperature=0.0, json_mode=True,
            max_tokens=max(int(config.LLM_MAX_OUTPUT_TOKENS), 1024))
    except LLMError as exc:
        log.warning("family router failed: %s", exc)
        return None
    obj = parse_json_object(text) or {}
    pick = str(obj.get("family") or "").strip()
    if not pick or pick.lower() in ("null", "none"):
        return None
    note = next((n for n in notes if n.id == pick or n.id.endswith("/" + pick) or n.name == pick), None)
    if note is None:
        log.info("family router picked %s, not a candidate", pick)
        return None
    match = routed(note, obj.get("values") or {}, question)
    if match is None:
        log.info("family router picked %s but its values did not fill the template", note.id)
    return match
