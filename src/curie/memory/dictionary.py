"""The dictionary index (knowledge plan F10, P10): the data dictionary of
the tables that matter, read in the background and searched locally.

    index = build(ddic, names, texts)      # worker, with SAP
    hits = search(index, "polisa VIN")     # the sap_find tool, no SAP
    line = render_table(index, "ZORDERS")  # a compact line for the prompt

`index` is the bundle's dictionary.json:

    {"built": <ts>, "langu": "L",
     "tables": {"ZORDERS": {"text": "Zamówienia", "text_en": "Sales orders",
                            "class": "TRANSP",
                            "fields": [{"n": "GJAHR", "t": "Rok", "k": 1, "d": "NUMC",
                                        "l": 4, "r": "ZROKUM", "c": ""}, ...]}}}

Search is lexical: words of the query by 5-character prefix (Polish
inflection) against table texts, field texts, and names as identifiers.
Every match names the table, its text and the matching fields, so the
model can go on with sap_read_table without guessing a field name.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict

from curie.memory.store import identifiers, prefixes

log = logging.getLogger(__name__)

NOISE_WORDS = {"tabel", "table", "pole", "field", "kolum", "colum"}
COPY = re.compile(r"archiw|kopia|copy|backup|wygen\.? tabela|generated table", re.IGNORECASE)
COPY_NAME = re.compile(r"_(?:A|ARCH|BAK|BACKUP|KOPIA\d*|OLD|TMP|TEMP|TEST|V)\d*$")


def usage_from(archive=None, store=None) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """How often each table and each field was used, from the archive (tool
    calls and programs that ran) and from the confirmed exemplars."""
    from curie.memory import shape as sh

    tables: dict[str, int] = defaultdict(int)
    fields: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def from_program(source: str) -> None:
        shape = sh.analyse(source or "")
        for t in shape.tables:
            tables[t] += 1
        for sel in shape.selects:
            for f, _op in sel.filters:
                fields[sel.table][f] += 1
        for lit in shape.literals:
            if lit.field:
                fields[lit.table][lit.field] += 1

    if store is not None:
        for n in store.notes():
            if n.type == "exemplar" and n.status == "confirmed" and n.program:
                from_program(n.program)
    if archive is not None:
        try:
            import json as _json
            for r in archive.query("SELECT name, args FROM tool_calls WHERE ok = 1"):
                args = _json.loads(r["args"] or "{}")
                table = str(args.get("table") or args.get("name") or "").upper()
                if not table:
                    continue
                tables[table] += 1
                for f in args.get("fields") or []:
                    fields[table][str(f).upper()] += 1
                for ident in identifiers(str(args.get("where") or "")):
                    fields[table][ident] += 1
            for r in archive.query("SELECT source FROM programs WHERE run_ok = 1"):
                from_program(r["source"])
        except Exception as exc:  # noqa: BLE001
            log.debug("usage from the archive: %s", exc)
    return dict(tables), {t: dict(f) for t, f in fields.items()}


def _norm(text: str) -> str:
    """Accents folded (curie/lexicon.py, the active languages), lower case."""
    from curie import lexicon

    return lexicon.fold(text).lower()


def build(ddic, names: list[str], texts: dict[str, dict] | None = None,
          langu: str = "L", log_every: int = 50) -> dict:
    """The index for `names` through a DdicCache-like object (structure(name)
    -> {"name", "fields": [FieldInfo]}); `texts` gives table texts
    {NAME: {"text": .., "text_en": .., "class": ..}}."""
    tables: dict[str, dict] = {}
    texts = texts or {}
    for i, name in enumerate(sorted({n.upper() for n in names if n})):
        try:
            info = ddic.structure(name)
        except Exception as exc:  # noqa: BLE001 -- a table that cannot be read is left out
            log.debug("dictionary: %s skipped: %s", name, exc)
            continue
        fields = []
        for f in info.get("fields", []):
            fields.append({"n": f.name, "t": (f.text or "")[:60], "k": 1 if f.key else 0,
                           "d": f.datatype or f.inttype, "l": f.intlen, "r": f.rollname or "",
                           "c": f.checktable or ""})
        t = texts.get(name, {})
        tables[name] = {"text": (t.get("text") or info.get("text") or "")[:80],
                        "text_en": (t.get("text_en") or "")[:80], "class": t.get("class", ""),
                        "fields": fields}
        if log_every and (i + 1) % log_every == 0:
            log.info("dictionary: %d table(s) read", i + 1)
    return {"built": time.time(), "langu": langu, "tables": tables}


class Index:
    """Word postings over a dictionary index, built in memory on load."""

    def __init__(self, data: dict):
        self.data = data or {}
        self.tables: dict[str, dict] = self.data.get("tables", {})
        # How often programs and tool calls used a table / a field (from the
        # archive and the notes): ranks the copies and archives below the
        # table people work with, and puts the used fields first.
        self.usage: dict[str, int] = {k.upper(): int(v) for k, v in (self.data.get("usage") or {}).items()}
        self.field_usage: dict[str, dict[str, int]] = {
            k.upper(): {f.upper(): int(n) for f, n in v.items()}
            for k, v in (self.data.get("field_usage") or {}).items()}
        self.words: dict[str, set[tuple]] = defaultdict(set)
        for name, t in self.tables.items():
            for w in prefixes(_norm(t.get("text", "") + " " + t.get("text_en", ""))):
                self.words[w].add(("T", name))
            for f in t.get("fields", []):
                for w in prefixes(_norm(f.get("t", ""))):
                    self.words[w].add(("F", name, f["n"]))

    def __len__(self) -> int:
        return len(self.tables)

    def search(self, query: str, limit: int = 8, kind: str = "any") -> dict:
        q_words = {w for w in prefixes(_norm(query)) if w not in NOISE_WORDS}
        q_idents = identifiers(query)
        table_score: dict[str, float] = defaultdict(float)
        matched_fields: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        matched_words: dict[str, set] = defaultdict(set)
        for w in q_words:
            for hit in self.words.get(w, ()):
                if hit[0] == "T":
                    table_score[hit[1]] += 3
                    matched_words[hit[1]].add(w)
                else:
                    table_score[hit[1]] += 1
                    matched_fields[hit[1]][hit[2]].add(w)
                    matched_words[hit[1]].add(w)
        for ident in q_idents:
            if ident in self.tables:
                table_score[ident] += 5
                matched_words[ident].add(ident)
            for name, t in self.tables.items():
                if any(f["n"] == ident for f in t.get("fields", [])):
                    table_score[name] += 2
                    matched_fields[name][ident].add(ident)
        for name in list(table_score):
            table_score[name] += min(self.usage.get(name, 0), 20) * 0.5
            if COPY.search(self.tables[name].get("text", "")) or COPY_NAME.search(name):
                table_score[name] -= 1.5
        ranked = sorted(table_score.items(), key=lambda kv: (-kv[1], kv[0]))
        # Copies and archives of a table (ZORDERS_A, ZORDERS_COPY2) are
        # listed under the table they copy, not beside it.
        main: list[tuple[str, float]] = []
        variants: dict[str, list[str]] = defaultdict(list)
        for name, score in ranked:
            parent = next((m for m, _s in main if name.startswith(m + "_")), None)
            if parent is not None:
                variants[parent].append(name)
                continue
            main.append((name, score))
        out = []
        for name, score in main[:limit]:
            t = self.tables[name]
            fields = t.get("fields", [])
            used = self.field_usage.get(name, {})
            chosen = [f for f in fields if f["n"] in matched_fields[name]]
            if kind != "field":
                chosen += [f for f in fields if f.get("k") and f not in chosen][:6]
                chosen += sorted((f for f in fields if f not in chosen and used.get(f["n"])),
                                 key=lambda f: -used[f["n"]])[:4]
            out.append({"name": name, "text": t.get("text") or t.get("text_en", ""),
                        "class": t.get("class", ""), "field_count": len(fields), "score": round(score, 1),
                        "used_by": self.usage.get(name, 0), "matched": sorted(matched_words[name]),
                        "variants": variants.get(name, [])[:6],
                        "fields": [{"name": f["n"], "text": f.get("t", ""), "type": f.get("d", ""),
                                    "length": f.get("l", 0), "key": bool(f.get("k")),
                                    "check_table": f.get("c", "")} for f in chosen[:14]]})
        note = "" if out else "no table or field text contains these words; try other words or sap_read_table on DD02T"
        return {"query": query, "tables": out, "note": note, "indexed_tables": len(self.tables)}

    def render_table(self, name: str, max_fields: int = 12) -> str:
        t = self.tables.get(name.upper())
        if not t:
            return ""
        fields = t.get("fields", [])
        keys = [f["n"] for f in fields if f.get("k")]
        used = self.field_usage.get(name.upper(), {})
        rest = [f for f in fields if not f.get("k")]
        # the fields programs and reads used first, then the table's own order
        shown = sorted(rest, key=lambda f: (-used.get(f["n"], 0), fields.index(f)))[:max_fields]
        parts = [f"table {name.upper()}" + (f" ({t['text']})" if t.get("text") else "") + ":"]
        if keys:
            parts.append("keys " + ", ".join(keys) + ";")
        parts.append("fields " + ", ".join(
            f"{f['n']}" + (f" ({f['t']})" if f.get("t") else "") for f in shown))
        if len(fields) > len(keys) + len(shown):
            parts.append(f"... {len(fields)} fields in all (sap_get_structure lists them)")
        return " ".join(parts)


def table_names_from(texts_rows: list[dict]) -> list[str]:
    return sorted({str(r.get("TABNAME", "")).strip().upper() for r in texts_rows if r.get("TABNAME")})


def is_customer_table(name: str) -> bool:
    return bool(re.match(r"^[ZY][A-Z0-9_]{2,}$", name or ""))
