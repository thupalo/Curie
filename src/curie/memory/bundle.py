"""The compiled bundle: what the runtime loads instead of parsing notes on
the hot path (knowledge plan 5.8).

    knowledge/.build/
      manifest.json   what was built from which sources, when, by whom
      index.json      lexical postings: identifiers and word prefixes -> note ids
      co_read.json    which tables confirmed programs read together
      templates.json  catalogue templates by question signature
      card.md         the system card (worker or person; see Knowledge.card)
      glossary.json   business term -> tables/fields/families (worker, K3)
      families.json   report families with typed parameters (worker, K3)
      graph.json      the schema graph (worker, K4)
      pitfalls.json   consolidated pitfalls with triggers (worker, K6)

Two kinds of parts: *lexical* parts are deterministic and cheap, rebuilt
in-process whenever the notes changed (so the Obsidian edit-and-see loop
keeps working); *derived* parts come from the consolidation worker and
stay as they are until it runs again. A missing or stale bundle is never
an error: the store answers on its own.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from curie.memory.store import KnowledgeStore, identifiers, prefixes, template_signature

log = logging.getLogger(__name__)

BUNDLE_VERSION = 1
LEXICAL_PARTS = ("index.json", "co_read.json", "templates.json")
DERIVED_PARTS = ("card.md", "glossary.json", "families.json", "graph.json", "pitfalls.json",
                 "dictionary.json")


@dataclass
class Bundle:
    index: dict = field(default_factory=dict)      # {"identifiers": {..}, "prefixes": {..}, "notes": {..}}
    co_read: dict = field(default_factory=dict)    # {"tables": {T: {partner: n}}, "counts": {T: n}}
    templates: dict = field(default_factory=dict)  # {signature: [note ids]}
    card: str = ""
    glossary: dict = field(default_factory=dict)   # {term: {"tables": [..], "fields": [..], "families": [..]}}
    families: dict = field(default_factory=dict)   # {family id: {...}}
    graph: dict = field(default_factory=dict)
    pitfalls: dict = field(default_factory=dict)
    dictionary: dict = field(default_factory=dict)  # memory/dictionary.py index data
    manifest: dict = field(default_factory=dict)

    @property
    def sources(self) -> str:
        return str(self.manifest.get("sources", ""))

    def summary(self) -> dict:
        return {"built": self.manifest.get("built"), "notes": len(self.index.get("notes", {})),
                "identifiers": len(self.index.get("identifiers", {})),
                "prefixes": len(self.index.get("prefixes", {})),
                "templates": sum(len(v) for v in self.templates.values()),
                "card": bool(self.card), "glossary": len(self.glossary),
                "families": len(self.families), "graph": bool(self.graph),
                "pitfalls": len(self.pitfalls.get("pitfalls", self.pitfalls) or []),
                "dictionary": len(self.dictionary.get("tables", {})),
                "derived_by": self.manifest.get("derived_by", "")}


def sources_hash(store: KnowledgeStore) -> str:
    """One hash over every note's id, mtime and status: the bundle's lexical
    parts are valid while it does not change."""
    h = hashlib.sha1()
    for n in sorted(store.notes(include_retired=True), key=lambda n: n.id):
        h.update(f"{n.id}|{n.mtime}|{n.status}\n".encode("utf-8"))
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Building the lexical parts
# --------------------------------------------------------------------------- #


def build_lexical(store: KnowledgeStore, bundle: Bundle | None = None) -> Bundle:
    """The deterministic parts from the notes; derived parts of `bundle`
    are carried over unchanged."""
    out = bundle or Bundle()
    ids: dict[str, set[str]] = {}
    pre: dict[str, set[str]] = {}
    meta: dict[str, dict] = {}
    for n in store.notes():
        meta[n.id] = {"type": n.type, "status": n.status, "tables": list(n.tables),
                      "catalogue": bool(n.catalogue), "size": len(n.body),
                      "family": getattr(n, "family", "") or ""}
        for ident in [n.name.upper()] + list(n.tables):
            if ident:
                ids.setdefault(ident, set()).add(n.id)
        words = set()
        for phrase in list(n.aliases) + list(n.keywords):
            words |= prefixes(phrase)
        if n.type == "exemplar" and n.question:
            words |= prefixes(n.question)
        if n.type in ("table", "concept"):
            words |= prefixes(n.name)
        for w in words:
            pre.setdefault(w, set()).add(n.id)
    out.index = {"identifiers": {k: sorted(v) for k, v in sorted(ids.items())},
                 "prefixes": {k: sorted(v) for k, v in sorted(pre.items())},
                 "notes": meta}
    # co-read statistics over confirmed programs
    exemplars = [n for n in store.notes() if n.type == "exemplar" and n.tables]
    counts: Counter = Counter()
    pairs: dict[str, Counter] = {}
    for n in exemplars:
        for t in n.tables:
            counts[t] += 1
            for other in n.tables:
                if other != t:
                    pairs.setdefault(t, Counter())[other] += 1
    out.co_read = {"counts": dict(counts),
                   "tables": {t: dict(c.most_common(6)) for t, c in sorted(pairs.items())}}
    templates: dict[str, list[str]] = {}
    for n in store.notes():
        if n.type == "exemplar" and n.catalogue and n.status == "confirmed" and n.program:
            templates.setdefault(template_signature(n.question), []).append(n.id)
    out.templates = templates
    out.manifest = {**out.manifest, "version": BUNDLE_VERSION, "built": time.time(),
                    "sources": sources_hash(store), "lexical_by": "in-process",
                    "notes": len(meta)}
    return out


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save(bundle: Bundle, directory: str | Path, parts: tuple = LEXICAL_PARTS + DERIVED_PARTS) -> None:
    """Write the named parts and the manifest, each file replaced atomically."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"index.json": bundle.index, "co_read.json": bundle.co_read,
               "templates.json": bundle.templates, "glossary.json": bundle.glossary,
               "families.json": bundle.families, "graph.json": bundle.graph,
               "pitfalls.json": bundle.pitfalls, "dictionary.json": bundle.dictionary}
    for name in parts:
        if name == "card.md":
            if bundle.card:
                _write_atomic(directory / name, bundle.card)
            continue
        value = payload.get(name)
        if value is None:
            continue
        if name in DERIVED_PARTS and not value:
            continue                      # never overwrite a derived part with nothing
        _write_atomic(directory / name, json.dumps(value, ensure_ascii=False, indent=0))
    _write_atomic(directory / "manifest.json", json.dumps(bundle.manifest, ensure_ascii=False, indent=1))


def load(directory: str | Path) -> Bundle | None:
    """The bundle on disk, or None when there is no manifest. A part that
    cannot be read is left empty (and rebuilt when lexical)."""
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return None
    bundle = Bundle()
    try:
        bundle.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("bundle manifest unreadable: %s", exc)
        return None
    if int(bundle.manifest.get("version", 0)) != BUNDLE_VERSION:
        log.info("bundle version %s differs from %s: rebuilding", bundle.manifest.get("version"),
                 BUNDLE_VERSION)
        bundle.manifest = {}
    for name, attr in (("index.json", "index"), ("co_read.json", "co_read"),
                       ("templates.json", "templates"), ("glossary.json", "glossary"),
                       ("families.json", "families"), ("graph.json", "graph"),
                       ("pitfalls.json", "pitfalls"), ("dictionary.json", "dictionary")):
        path = directory / name
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                setattr(bundle, attr, value)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("bundle part %s unreadable: %s", name, exc)
    card = directory / "card.md"
    if card.exists():
        try:
            bundle.card = card.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return bundle


def is_stale(bundle: Bundle | None, store: KnowledgeStore) -> bool:
    return bundle is None or not bundle.index or bundle.sources != sources_hash(store)


def ensure(store: KnowledgeStore, directory: str | Path, bundle: Bundle | None = None) -> Bundle:
    """The bundle for the current notes: loaded when fresh, otherwise the
    lexical parts are rebuilt in-process and saved; derived parts stay."""
    current = bundle or load(directory)
    if not is_stale(current, store):
        return current
    rebuilt = build_lexical(store, current)
    try:
        save(rebuilt, directory, parts=LEXICAL_PARTS)
    except OSError as exc:
        log.warning("bundle not saved to %s: %s", directory, exc)
    log.info("knowledge bundle rebuilt: %d note(s), %d identifier(s), %d template(s)",
             rebuilt.manifest.get("notes", 0), len(rebuilt.index.get("identifiers", {})),
             sum(len(v) for v in rebuilt.templates.values()))
    return rebuilt


def lookup(bundle: Bundle, text: str, tables: list[str] | None = None) -> set[str]:
    """Candidate note ids for a text through the lexical index (identifiers
    and word prefixes); the store scores them."""
    out: set[str] = set()
    ids = bundle.index.get("identifiers", {})
    pre = bundle.index.get("prefixes", {})
    for ident in identifiers(text) | {t.upper() for t in (tables or [])}:
        out.update(ids.get(ident, ()))
    for p in prefixes(text):
        out.update(pre.get(p, ()))
    return out
