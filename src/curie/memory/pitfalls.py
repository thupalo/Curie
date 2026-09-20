"""P7: the pitfall consolidator.

Pitfalls are the one-line insights Curie distils from fix rounds
(knowledge/insights/). The worker gives them structure from the archive:

* a `trigger` (the syntax word and message of the archived error the
  pitfall is about) when a keyword of the note equals a syntax word seen
  in the archive;
* one note per rule: insights that say the same thing (word-prefix Jaccard
  at or above MERGE_THRESHOLD) are merged into the one with the better
  record, the other retired with `superseded_by`;
* `counter_evidence`: the times the pitfall was in the prompt of an
  episode that still failed with its trigger (the review job and a person
  can retire a pitfall that does not help);
* the syntax clusters of the archive (word, message, count, whether a
  deterministic repair in sap_tools.abap_source now covers it): the
  "static rule candidates" of the plan, reported for the administrator;
* `pitfalls.json` in the bundle with all of it.

Everything here is deterministic; the model is not needed.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from curie.memory.store import KnowledgeStore, Note, identifiers, prefixes, write_note
from curie.sap_tools.abap_source import BLOCK_KEYWORDS

log = logging.getLogger(__name__)

MERGE_THRESHOLD = 0.6
# Syntax words a deterministic repair (abap_source.repair_source) or a
# dialect check (dialect_problems) now takes care of.
REPAIRED_WORDS = {"*", "UP", "DESC", "ASC"} | set(BLOCK_KEYWORDS)
DIALECT_WORDS = {"DATA(", "@", "VALUE", "CONV", "COND", "SWITCH"}


def syntax_clusters(archive) -> list[dict]:
    """The archived syntax errors grouped by word and message, most frequent
    first; `repaired` says a deterministic repair covers the word."""
    if archive is None:
        return []
    try:
        rows = archive.query("SELECT episode_id, data FROM events WHERE kind = 'syntax_error'")
    except Exception as exc:  # noqa: BLE001
        log.warning("pitfalls: archive not read: %s", exc)
        return []
    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        try:
            d = json.loads(r["data"] or "{}")
        except (TypeError, ValueError):
            continue
        word = str(d.get("word") or "").strip().upper()
        message = " ".join(str(d.get("message") or "").split())
        head = re.sub(r"\"[^\"]*\"", '"..."', message)[:80]
        g = groups.setdefault((word, head), {"word": word, "message": message[:160], "count": 0,
                                             "episodes": set()})
        g["count"] += 1
        g["episodes"].add(r["episode_id"])
    out = []
    for g in groups.values():
        word = g["word"]
        repaired = word in REPAIRED_WORDS or any(word.startswith(w) for w in DIALECT_WORDS)
        out.append({"word": word, "message": g["message"], "count": g["count"],
                    "episodes": len(g["episodes"]), "repaired": repaired,
                    "episode_ids": sorted(g["episodes"])})
    out.sort(key=lambda g: (-g["count"], g["word"]))
    return out


def _words_of(note: Note) -> set[str]:
    words = {k.upper() for k in note.keywords if k} | identifiers(note.body.splitlines()[0] if note.body else "")
    return {w for w in words if w}


def attach_triggers(insights: list[Note], clusters: list[dict]) -> int:
    """A trigger for every insight whose keywords name an archived syntax
    word; returns how many were set."""
    by_word = {}
    for c in clusters:
        by_word.setdefault(c["word"], c)
    changed = 0
    for n in insights:
        if n.trigger:
            continue
        for w in sorted(_words_of(n)):
            c = by_word.get(w)
            if c is not None:
                n.trigger = f"{c['word']}: {c['message']}"[:200]
                write_note(n)
                changed += 1
                break
    return changed


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def merge_duplicates(store: KnowledgeStore, insights: list[Note]) -> list[tuple[str, str]]:
    """Pairs (retired id, kept id) of insights that say the same thing."""
    merged: list[tuple[str, str]] = []
    alive = [n for n in insights if n.status != "retired"]
    done: set[str] = set()
    for i, a in enumerate(alive):
        if a.id in done:
            continue
        pa = prefixes(a.body.splitlines()[0] if a.body else "")
        for b in alive[i + 1:]:
            if b.id in done:
                continue
            pb = prefixes(b.body.splitlines()[0] if b.body else "")
            if _jaccard(pa, pb) < MERGE_THRESHOLD:
                continue
            keep, drop = sorted((a, b), key=lambda n: (-n.successes, n.failures, n.created, n.id))
            keep.uses += drop.uses
            keep.successes += drop.successes
            keep.failures += drop.failures
            for t in drop.tables:
                if t not in keep.tables:
                    keep.tables.append(t)
            for k in drop.keywords:
                if k not in keep.keywords and len(keep.keywords) < 8:
                    keep.keywords.append(k)
            if not keep.trigger and drop.trigger:
                keep.trigger = drop.trigger
            store.retire(drop.id, f"merged into {keep.id} by the consolidator (same rule)", keep.id)
            write_note(keep)
            merged.append((drop.id, keep.id))
            done.add(drop.id)
            if drop is a:
                break
    return merged


def counter_evidence(archive, insights: list[Note], clusters: list[dict]) -> int:
    """For every insight with a trigger: the episodes where it was in a
    prompt and the trigger's error happened anyway. Returns the notes
    changed."""
    if archive is None:
        return 0
    failing: dict[str, set[str]] = {}
    for c in clusters:
        failing.setdefault(c["word"], set()).update(c.get("episode_ids") or [])
    changed = 0
    for n in insights:
        if not n.trigger or n.status == "retired":
            continue
        word = n.trigger.split(":", 1)[0].strip().upper()
        episodes = failing.get(word)
        if not episodes:
            continue
        try:
            rows = archive.query("SELECT DISTINCT episode_id FROM calls WHERE notes LIKE ?", (f"%{n.id}%",))
        except Exception as exc:  # noqa: BLE001
            log.warning("pitfalls: calls not read: %s", exc)
            return changed
        shown = {r["episode_id"] for r in rows}
        count = len(shown & episodes)
        if count != n.counter_evidence:
            n.counter_evidence = count
            write_note(n)
            changed += 1
    return changed


def as_bundle(insights: list[Note], clusters: list[dict]) -> dict:
    """The pitfalls part of the bundle."""
    pitfalls = []
    for n in sorted(insights, key=lambda n: (n.status != "confirmed", -n.successes, n.id)):
        if n.status == "retired":
            continue
        pitfalls.append({"id": n.id, "text": (n.body.splitlines() or [""])[0][:300], "status": n.status,
                         "trigger": n.trigger, "keywords": list(n.keywords), "tables": list(n.tables),
                         "uses": n.uses, "successes": n.successes, "failures": n.failures,
                         "counter_evidence": n.counter_evidence})
    syntax = [{k: v for k, v in c.items() if k != "episode_ids"} for c in clusters]
    return {"built": time.time(), "pitfalls": pitfalls, "syntax": syntax}


def consolidate(store: KnowledgeStore, archive=None) -> dict:
    """Run every step; returns the report and leaves the bundle part in
    `report["bundle"]`."""
    clusters = syntax_clusters(archive)
    insights = [n for n in store.notes(include_retired=True) if n.type == "insight"]
    triggers = attach_triggers(insights, clusters)
    merged = merge_duplicates(store, insights)
    store.refresh(force=True)
    insights = [n for n in store.notes(include_retired=True) if n.type == "insight"]
    evidence = counter_evidence(archive, insights, clusters)
    store.refresh(force=True)
    insights = [n for n in store.notes(include_retired=True) if n.type == "insight"]
    part = as_bundle(insights, clusters)
    candidates = [c for c in clusters if not c["repaired"] and c["episodes"] >= 2]
    report = {"insights": len([n for n in insights if n.status != "retired"]), "triggers": triggers,
              "merged": merged, "counter_evidence": evidence,
              "syntax_clusters": len(clusters), "repaired_clusters": sum(1 for c in clusters if c["repaired"]),
              "rule_candidates": [{"word": c["word"], "message": c["message"], "episodes": c["episodes"]}
                                  for c in candidates], "bundle": part}
    log.info("pitfalls: %d insight(s), %d trigger(s) set, %d merged, %d with counter-evidence; "
             "%d syntax cluster(s), %d covered by repairs, %d rule candidate(s)",
             report["insights"], triggers, len(merged), evidence, len(clusters),
             report["repaired_clusters"], len(candidates))
    return report


def write_part(part: dict, build_dir: str | Path) -> Path:
    path = Path(build_dir) / "pitfalls.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(part, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(path)
    return path
