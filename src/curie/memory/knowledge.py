"""The Knowledge facade: the one interface the runtime has to knowledge
(knowledge plan, section 4).

    knowledge = Knowledge(store, build_dir)
    ctx = knowledge.context_for(question, tables, extra)   # -> PromptContext
    match = knowledge.match_family(question)               # -> (note, mapping) | None

Writers (add_hint, add_exemplar, add_insight, record, retire,
mark_catalogue ...) and the other readers (get, notes, search, catalogue,
find_template ...) are delegated to the store, so every existing caller
keeps working. What the facade adds on top of the store:

* the compiled bundle (memory/bundle.py): lexical parts rebuilt in-process
  when notes change, derived parts (card, glossary, families, graph,
  pitfalls) from the consolidation worker;
* the system card, injected into every prompt: `knowledge/CARD.md` when a
  person wrote one, else the worker's `.build/card.md`;
* glossary expansion: business terms of the question add their tables to
  the retrieval context before the store scores the notes;
* PromptContext: the rendered text plus the ids that went in, so the
  orchestrator records exactly what the model was given.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from curie.memory import bundle as bundle_mod
from curie.memory.store import KnowledgeStore, Note, prefixes

log = logging.getLogger(__name__)

CARD_FILE = "CARD.md"


@dataclass
class PromptContext:
    text: str = ""
    notes: list = field(default_factory=list)       # note ids in the prompt
    exemplars: list = field(default_factory=list)   # exemplar ids in the prompt
    family: str = ""                                # matched family id, when any
    card: bool = False                              # the system card was included
    glossary_tables: list = field(default_factory=list)
    dictionary: list = field(default_factory=list)  # tables rendered from the dictionary index


class Knowledge:
    def __init__(self, store: KnowledgeStore, build_dir: str | Path | None = None,
                 bundle_check_every: float = 3.0):
        self.store = store
        self.build_dir = Path(build_dir) if build_dir else Path(store.root) / ".build"
        self.bundle_check_every = bundle_check_every
        self._bundle: bundle_mod.Bundle | None = None
        self._checked_at = 0.0
        self._card_text = ""
        self._card_mtime = -1.0
        self.ensure_bundle(force=True)

    # -- delegation -------------------------------------------------------------- #

    def __getattr__(self, name):
        # Everything not defined here is the store's (writers, readers,
        # table_exists ...); tests and commands keep their calls.
        store = self.__dict__.get("store")
        if store is None:
            raise AttributeError(name)
        return getattr(store, name)

    def __setattr__(self, name, value):
        if name == "table_exists" and "store" in self.__dict__:
            self.store.table_exists = value
            return
        super().__setattr__(name, value)

    def __len__(self) -> int:
        return len(self.store)

    # -- the bundle ------------------------------------------------------------------ #

    @property
    def bundle(self) -> bundle_mod.Bundle:
        self.ensure_bundle()
        return self._bundle

    def ensure_bundle(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._checked_at < self.bundle_check_every:
            return
        self._checked_at = now
        try:
            self._bundle = bundle_mod.ensure(self.store, self.build_dir, self._bundle)
        except Exception as exc:  # noqa: BLE001 -- the store answers without a bundle
            log.warning("knowledge bundle: %s", exc)
            if self._bundle is None:
                self._bundle = bundle_mod.Bundle()

    def reload_bundle(self) -> dict:
        """Take the bundle from disk again (after the worker wrote derived parts)."""
        self._bundle = bundle_mod.load(self.build_dir)
        self._dictionary = None
        self.ensure_bundle(force=True)
        return self._bundle.summary()

    # -- the dictionary index --------------------------------------------------------- #

    @property
    def dictionary(self):
        """memory/dictionary.Index over the bundle's dictionary.json (empty
        until the worker built it with SAP)."""
        from curie.memory.dictionary import Index

        data = self.bundle.dictionary
        cached = self.__dict__.get("_dictionary")
        if cached is None or cached.data is not data:
            cached = Index(data)
            self.__dict__["_dictionary"] = cached
        return cached

    def find(self, query: str, limit: int = 8, kind: str = "any") -> dict:
        """The sap_find tool: tables and fields by words, from the index."""
        index = self.dictionary
        if not len(index):
            return {"query": query, "tables": [], "indexed_tables": 0,
                    "note": "the dictionary index is not built yet (curie --consolidate with SAP); "
                            "use sap_get_structure or read DD02T"}
        return index.search(query, limit=limit, kind=kind)

    def neighbourhood(self, tables: list[str], max_tables: int = 3) -> list[str]:
        """Compact dictionary lines for the tables in play (plan 9.2)."""
        index = self.dictionary
        if not len(index):
            return []
        out = []
        for t in tables[:max_tables]:
            line = index.render_table(t)
            if line:
                out.append(line)
        return out

    # -- the system card --------------------------------------------------------------- #

    @property
    def card(self) -> str:
        """`knowledge/CARD.md` when a person wrote one, else the bundle's card."""
        path = Path(self.store.root) / CARD_FILE
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = -1.0
        if mtime != self._card_mtime:
            self._card_mtime = mtime
            try:
                self._card_text = path.read_text(encoding="utf-8").strip() if mtime >= 0 else ""
            except OSError:
                self._card_text = ""
        return self._card_text or self.bundle.card

    # -- reading for a turn ---------------------------------------------------------- #

    def glossary_tables(self, text: str) -> list[str]:
        """Tables the glossary attaches to business terms in the text."""
        glossary = self.bundle.glossary
        if not glossary:
            return []
        ctx = prefixes(text)
        found: set[str] = set()
        for term, entry in glossary.items():
            words = prefixes(term)
            if words and words <= ctx:
                found.update(str(t).upper() for t in (entry.get("tables") or []))
        return sorted(found)

    def context_for(self, text: str, tables: list[str] | None = None, extra: str = "") -> PromptContext:
        """The knowledge block for one model call, and what went into it."""
        ctx = PromptContext()
        full = f"{text}\n{extra}" if extra else text
        expanded = sorted({t.upper() for t in (tables or [])})
        ctx.glossary_tables = [t for t in self.glossary_tables(full) if t not in expanded]
        expanded += ctx.glossary_tables
        notes, exemplars = self.store.retrieve(full, expanded)
        ctx.notes = [n.id for n in notes]
        ctx.exemplars = [n.id for n in exemplars]
        parts = []
        card = self.card
        if card:
            ctx.card = True
            parts.append("About this system (the system card):\n" + card)
        rendered = self.store.render(notes, exemplars, self.store.co_read(expanded))
        if rendered:
            parts.append(rendered)
        # The tables in play, from the dictionary index: keys and fields
        # with texts, so a known table costs no structure call.
        in_play = [t for t in expanded if t]
        for n in notes:
            for t in n.tables:
                if t not in in_play:
                    in_play.append(t)
        lines = self.neighbourhood(in_play)
        if lines:
            ctx.dictionary = [l.split()[1] for l in lines]
            parts.append("From the data dictionary (fields are exact names):\n" + "\n".join(f"- {l}" for l in lines))
        ctx.text = "\n\n".join(parts)
        return ctx

    def match_family(self, question: str):
        """A replayable template for the question (memory/families.py
        FamilyMatch): a confirmed report family whose typed parameters the
        question fills, else a catalogue exemplar with the same literal
        signature; None otherwise."""
        from curie.memory import families

        match = families.match(self.store, question)
        if match is not None:
            return match
        found = self.store.find_template(question)
        if found is None:
            return None
        note, mapping = found
        return families.FamilyMatch(note=note, program=self.store.instantiate(note, mapping),
                                    values=dict(mapping), score=1.0, kind="catalogue")

    async def route_family(self, question: str, llm, model: str | None = None):
        """The model as a router (plan 8.3) when nothing matched lexically:
        among the families that share a table or words with the question,
        the model picks one or none and names the values; used only when
        the values are in the question and fill the template."""
        from curie.memory import families

        tables = self.glossary_tables(question)
        return await families.route(self.store, question, llm, model=model, tables=tables)

    def search(self, query: str, limit: int = 8) -> list[Note]:
        """The sap_knowledge tool: the store's search, candidates first from
        the lexical index when the bundle has one."""
        found = self.store.search(query, limit)
        if len(found) >= limit or not self.bundle.index:
            return found
        seen = {n.id for n in found}
        for nid in sorted(bundle_mod.lookup(self.bundle, query)):
            if nid in seen:
                continue
            note = self.store.get(nid)
            if note is not None and note.status != "retired":
                found.append(note)
                seen.add(nid)
            if len(found) >= limit:
                break
        return found

    def stats(self) -> dict:
        return {"notes": len(self.store), "bundle": self.bundle.summary(), "dictionary": len(self.dictionary),
                "card": "file" if self._card_text else ("bundle" if self.bundle.card else "none")}
