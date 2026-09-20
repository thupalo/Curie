"""The glossary miner (knowledge plan 5.5, 7 P5): business words of the
questions aligned with the tables the confirmed programs read.

For every confirmed episode (question, program) the question's word
prefixes are paired with the program's tables. A word that co-occurs with
a table in `min_support` episodes and in at least `min_ratio` of the
episodes it appears in at all is a glossary term for that table. Table
names themselves, stop words and question verbs are left out. The result
seeds `glossary.json` in the bundle; the store's own aliases stay the
human-written part.

    entries = mine(pairs)      # pairs: [(question, [tables])]
    {"order": {"tables": ["ZORDERS"], "support": 12, "forms": ["orders", "order"]}}
"""
from __future__ import annotations

from collections import Counter, defaultdict

from curie import lexicon
from curie.memory.store import WORD, prefixes

# Language-dependent: question words, verbs and function words of the
# active languages, from curie/lexicon.py.
STOP = lexicon.merged_set("stopwords")
STOP_PREFIXES = {w[:5] for w in STOP}


def question_terms(question: str) -> dict[str, str]:
    """Prefix -> the word as written, for the content words of a question."""
    out: dict[str, str] = {}
    for w in WORD.findall(question or ""):
        lw = w.lower()
        if len(lw) < 4 or lw in STOP:
            continue
        p = lw[:5]
        if p in STOP_PREFIXES or p.upper() == p:
            continue
        out.setdefault(p, w)
    return out


def mine(pairs: list[tuple[str, list[str]]], min_support: int = 2, min_ratio: float = 0.6,
         max_tables: int = 3) -> dict[str, dict]:
    """Glossary entries from (question, tables) pairs of confirmed episodes."""
    term_total: Counter = Counter()
    term_table: dict[str, Counter] = defaultdict(Counter)
    forms: dict[str, set[str]] = defaultdict(set)
    table_names = {t.upper() for _q, tables in pairs for t in tables}
    for question, tables in pairs:
        tables = sorted({t.upper() for t in tables})
        if not tables:
            continue
        terms = question_terms(question)
        for p, word in terms.items():
            if word.upper() in table_names:
                continue
            term_total[p] += 1
            forms[p].add(word)
            for t in tables:
                term_table[p][t] += 1
    out: dict[str, dict] = {}
    for p, total in term_total.items():
        if total < min_support:
            continue
        chosen = [(t, c) for t, c in term_table[p].most_common(max_tables)
                  if c >= min_support and c / total >= min_ratio]
        if not chosen:
            continue
        out[p] = {"tables": [t for t, _c in chosen], "support": total,
                  "forms": sorted(forms[p], key=str.lower)[:6]}
    return dict(sorted(out.items()))


def merge(existing: dict, mined: dict) -> dict:
    """Mined entries never overwrite what a person confirmed (`confirmed: true`)."""
    out = dict(mined)
    for term, entry in (existing or {}).items():
        if entry.get("confirmed"):
            out[term] = entry
    return out
