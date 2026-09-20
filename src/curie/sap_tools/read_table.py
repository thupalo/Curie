"""RFC_READ_TABLE with automatic field selection and buffer management.

The function module's limits, and what this module does about them:

* DATA is a table of TAB512 -- one 512-character line per row. A row wider
  than that ends the call with DATA_BUFFER_EXCEEDED. The FIELDS list is
  therefore planned against the DDIC field widths (key fields first, then in
  position order) and, should the backend still refuse, trimmed from the end
  and retried.
* OPTIONS lines are CHAR72 and are concatenated into the WHERE clause, so a
  token or a quoted literal must never be split across lines.
* FLTP fields are refused by older releases (FIELD_NOT_VALID); they are left
  out of automatic selection and reported.
* The result is one fixed-offset string per row; FIELDS comes back with
  OFFSET/LENGTH/TYPE, which is what parse_rows() uses.

Pure helpers (plan_fields, pack_options, parse_rows) have no SAP dependency
and are unit-tested; read_table() does the calls.
"""
from __future__ import annotations

import difflib
import fnmatch
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from curie import config
from curie.caches import TTLCache
from curie.sapnwrfc720 import ABAPException, RFCError

log = logging.getLogger(__name__)

LINE_WIDTH = 512
OPTION_WIDTH = 72

# ABAP internal type (DD03L-INTTYPE / RFC_FIELDS-EXID) -> estimated output
# width in characters for a field of internal length n. Overestimating a
# packed field only costs a field in automatic selection; underestimating
# would cost the whole call.
def estimate_width(inttype: str, intlen: int, decimals: int = 0) -> int | None:
    t = (inttype or "C").upper()
    if t in ("C", "N", "D", "T", "G"):
        return max(intlen, 1)
    if t == "X":
        return max(intlen * 2, 2)
    if t == "P":
        return 2 * max(intlen, 1) + 1 + (1 if decimals else 0)
    if t == "I":
        return 11
    if t == "B":
        return 3
    if t == "S":
        return 6
    if t == "8":  # INT8
        return 20
    if t == "F":
        return None  # not readable with RFC_READ_TABLE on old releases
    return max(intlen, 1)


@dataclass
class FieldInfo:
    name: str
    position: int = 0
    key: bool = False
    inttype: str = "C"      # ABAP internal type letter
    intlen: int = 0
    decimals: int = 0
    datatype: str = ""      # DDIC type (CHAR, NUMC, DATS, CUKY, ...)
    text: str = ""
    rollname: str = ""
    checktable: str = ""

    @property
    def width(self) -> int | None:
        return estimate_width(self.inttype, self.intlen, self.decimals)


@dataclass
class ReadPlan:
    fields: list[str]
    omitted: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)
    width: int = 0


class ReadTableError(Exception):
    """A problem the LLM can act on (the message says what)."""


SQL_WORDS = {"AND", "OR", "NOT", "LIKE", "IN", "BETWEEN", "IS", "NULL", "INITIAL", "EQ", "NE",
             "GT", "LT", "GE", "LE", "CP", "NP", "ESCAPE", "TRUE", "FALSE"}
WHERE_TOKEN = re.compile(r"'[^']*'|\b[A-Za-z_][A-Za-z0-9_]*\b")


def suggest(name: str, fields: list[FieldInfo], limit: int = 3) -> list[str]:
    """Fields a guessed name probably meant: close names, then matching texts."""
    name = name.strip().upper()
    names = [f.name for f in fields]
    out = difflib.get_close_matches(name, names, n=limit, cutoff=0.6)
    stem = name.rstrip("0123456789_")[:4].lower()
    if len(stem) >= 3:
        for f in fields:
            if len(out) >= limit:
                break
            if f.name not in out and (stem in f.name.lower() or stem in (f.text or "").lower()):
                out.append(f.name)
    return out


def describe_unknown(name: str, fields: list[FieldInfo]) -> str:
    hints = suggest(name, fields)
    return f"{name} is not a field" + (f" (did you mean {', '.join(hints)}?)" if hints else "")


def field_list(fields: list[FieldInfo], limit: int = 80) -> str:
    """Names with texts, compact enough for a tool result."""
    items = [f"{f.name}" + (f" ({f.text})" if f.text else "") for f in fields[:limit]]
    more = f", ... {len(fields) - limit} more" if len(fields) > limit else ""
    return ", ".join(items) + more


def unknown_in_where(where, fields: list[FieldInfo]) -> list[str]:
    """Identifiers of a WHERE clause that are not fields of the table."""
    text = where if isinstance(where, str) else " AND ".join(w for w in (where or []) if w)
    if not text:
        return []
    known = {f.name.upper() for f in fields}
    out = []
    for m in WHERE_TOKEN.finditer(text):
        token = m.group(0)
        if token.startswith("'"):
            continue
        upper = token.upper()
        if upper in SQL_WORDS or upper in known or upper in out or upper.isdigit():
            continue
        out.append(upper)
    return out


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def denied(table: str, patterns: list[str] | None = None) -> bool:
    table = table.upper()
    for pattern in (config.READ_TABLE_DENY if patterns is None else patterns):
        if fnmatch.fnmatchcase(table, pattern.upper()):
            return True
    return False


def plan_fields(fields: list[FieldInfo], requested: list[str] | None,
                limit: int = LINE_WIDTH) -> ReadPlan:
    """Choose the FIELDS list so that one row fits the 512-character line.

    With `requested`, every name is validated and the total width checked;
    an overflow is an error listing the widths so the caller can narrow.
    Without, key fields go first, then the rest in position order while they
    fit; FLTP and long RAW fields are skipped and reported.
    """
    by_name = {f.name.upper(): f for f in fields}
    if requested:
        chosen, unknown, total = [], [], 0
        for name in requested:
            f = by_name.get(name.strip().upper())
            if f is None:
                unknown.append(name)
                continue
            w = f.width
            if w is None:
                raise ReadTableError(
                    f"field {f.name} is a floating point field; RFC_READ_TABLE "
                    "cannot return it, leave it out")
            chosen.append(f)
            total += w
        if unknown and not chosen:
            # Nothing usable was asked for: say what exists.
            raise ReadTableError(
                "unknown field(s): " + "; ".join(describe_unknown(u, fields) for u in unknown)
                + ". Fields of the table: " + field_list(fields))
        if total > limit:
            widths = ", ".join(f"{f.name}({f.width})" for f in chosen)
            raise ReadTableError(
                f"the requested fields need {total} characters per row, the "
                f"limit is {limit}; drop some: {widths}")
        # Guessed names are dropped, not fatal: the rows come back with a
        # note naming them and the likely field, so a round trip is saved.
        omitted = [(u, describe_unknown(u, fields)) for u in unknown]
        return ReadPlan([f.name for f in chosen], omitted, total)

    ordered = sorted(fields, key=lambda f: (not f.key, f.position))
    chosen: list[str] = []
    omitted: list[tuple[str, str]] = []
    total = 0
    for f in ordered:
        w = f.width
        if w is None:
            omitted.append((f.name, "floating point, not readable"))
            continue
        if f.inttype.upper() == "X" and f.intlen > 32:
            omitted.append((f.name, "long RAW"))
            continue
        if total + w > limit:
            omitted.append((f.name, f"no room ({w} chars)"))
            continue
        chosen.append(f.name)
        total += w
    if not chosen:
        raise ReadTableError("no field of the table fits the 512-character row")
    # Keep DDIC order in the call so the columns read naturally.
    chosen.sort(key=lambda n: by_name[n].position)
    return ReadPlan(chosen, omitted, total)


_TOKEN = re.compile(r"""'(?:[^']|'')*'|`(?:[^`]|``)*`|\S+""")


def tokenize_where(where: str) -> list[str]:
    """Split a WHERE clause into tokens, keeping quoted literals whole."""
    return _TOKEN.findall(where)


def pack_options(where: str | list[str] | None, width: int = OPTION_WIDTH) -> list[dict]:
    """OPTIONS rows for RFC_READ_TABLE: 72-char lines that never split a token."""
    if not where:
        return []
    if isinstance(where, list):
        parts = [w.strip() for w in where if w and w.strip()]
        text = " AND ".join(f"( {p} )" if len(parts) > 1 else p for p in parts)
    else:
        text = where.strip()
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for token in tokenize_where(text):
        if len(token) > width:
            raise ReadTableError(
                f"a single token in the WHERE clause is longer than {width} "
                f"characters: {token[:40]}...")
        if not current:
            current = token
        elif len(current) + 1 + len(token) <= width:
            current += " " + token
        else:
            lines.append(current)
            current = token
    if current:
        lines.append(current)
    return [{"TEXT": line} for line in lines]


# --------------------------------------------------------------------------- #
# Parsing the result
# --------------------------------------------------------------------------- #


def _num(text: str) -> int:
    try:
        return int(str(text).strip() or 0)
    except ValueError:
        return 0


def convert_value(raw: str, rfc_type: str) -> object:
    """A DATA slice to a JSON-friendly value, by the TYPE RFC_READ_TABLE gave."""
    t = (rfc_type or "C").upper()
    if t == "D":
        v = raw.strip()
        if not v or v == "00000000":
            return ""
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}" if len(v) == 8 and v.isdigit() else v
    if t == "T":
        v = raw.strip()
        if not v or v == "000000":
            return ""
        return f"{v[:2]}:{v[2:4]}:{v[4:6]}" if len(v) == 6 and v.isdigit() else v
    if t in ("I", "B", "S", "8"):
        v = raw.strip()
        if not v:
            return 0
        neg = v.endswith("-")
        v = v.rstrip("-").lstrip("+").strip()
        try:
            n = int(v)
        except ValueError:
            return raw.strip()
        return -n if neg else n
    if t == "P":
        v = raw.strip()
        if not v:
            return "0"
        neg = v.endswith("-")
        v = v.rstrip("-").strip()
        # The RFC user's decimal notation may be a comma.
        if "," in v and "." not in v:
            v = v.replace(",", ".")
        elif "," in v and "." in v:
            v = v.replace(".", "").replace(",", ".")
        try:
            d = Decimal(v)
        except InvalidOperation:
            return raw.strip()
        return format(-d if neg else d, "f")
    if t == "N":
        return raw.strip()
    return raw.rstrip()


def parse_rows(data_rows: list[dict], fields_out: list[dict]) -> tuple[list[dict], list[list]]:
    """Split each DATA.WA line by the OFFSET/LENGTH the backend returned."""
    columns = []
    for f in fields_out:
        columns.append({
            "name": str(f.get("FIELDNAME", "")).strip(),
            "offset": _num(f.get("OFFSET", 0)),
            "length": _num(f.get("LENGTH", 0)),
            "type": str(f.get("TYPE", "C")).strip() or "C",
            "text": str(f.get("FIELDTEXT", "")).strip(),
        })
    rows = []
    for r in data_rows:
        line = str(r.get("WA", ""))
        row = []
        for c in columns:
            raw = line[c["offset"]:c["offset"] + c["length"]]
            row.append(convert_value(raw, c["type"]))
        rows.append(row)
    return columns, rows


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #


def raw_read_table(sap, table: str, fields: list[str], options: list[dict],
                   rowcount: int = 0, rowskips: int = 0) -> dict:
    """One RFC_READ_TABLE call; returns the raw FIELDS and DATA tables."""
    return sap.call_known(
        "RFC_READ_TABLE",
        QUERY_TABLE=table.upper(),
        DELIMITER="",
        NO_DATA="",
        ROWSKIPS=rowskips,
        ROWCOUNT=rowcount,
        OPTIONS=options,
        FIELDS=[{"FIELDNAME": f} for f in fields],
        DATA=[],
    )


def read_rows(sap, table: str, fields: list[str], where=None,
              rowcount: int = 0, rowskips: int = 0) -> tuple[list[dict], list[list]]:
    """Convenience for internal reads (DD03L, TBTCO, TRDIR): columns, rows."""
    out = raw_read_table(sap, table, fields, pack_options(where), rowcount, rowskips)
    return parse_rows(out.get("DATA", []), out.get("FIELDS", []))


def rows_as_dicts(columns: list[dict], rows: list[list]) -> list[dict]:
    names = [c["name"] for c in columns]
    return [dict(zip(names, r)) for r in rows]


class ReadTableTool:
    """The LLM-facing table read, with the DDIC cache and a result cache."""

    def __init__(self, sap, ddic, ttl: float | None = None):
        self.sap = sap
        self.ddic = ddic
        self.ttl = config.READ_TABLE_CACHE_TTL if ttl is None else ttl
        self._cache = TTLCache("read_table_results", self.ttl, config.CACHE_MAX_ENTRIES)

    def read(self, table: str, fields: list[str] | None = None, where=None,
             max_rows: int | None = None, skip: int = 0) -> dict:
        table = (table or "").strip().upper()
        if not table:
            raise ReadTableError("table name is required")
        if denied(table):
            raise ReadTableError(f"reading {table} is not allowed by policy")
        if max_rows is None or max_rows <= 0:
            max_rows = config.READ_TABLE_DEFAULT_ROWS
        max_rows = min(int(max_rows), config.READ_TABLE_MAX_ROWS)
        skip = max(0, int(skip or 0))
        key = (table, tuple(f.upper() for f in (fields or [])),
               where if isinstance(where, str) else tuple(where or []), max_rows, skip)
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        info = self.ddic.structure(table)
        bad = unknown_in_where(where, info["fields"])
        if bad:
            # A condition on a field that does not exist cannot be dropped
            # without changing the question: stop before the RFC call.
            raise ReadTableError(
                "the WHERE clause uses " + "; ".join(describe_unknown(b, info["fields"]) for b in bad)
                + ". Fields of the table: " + field_list(info["fields"]))
        plan = plan_fields(info["fields"], fields)
        options = pack_options(where)
        notes = [f"omitted {name}: {why}" for name, why in plan.omitted]
        if plan.omitted and fields:
            notes.append("fields of the table: " + field_list(info["fields"], limit=60))

        chosen = list(plan.fields)
        attempts = 0
        while True:
            try:
                out = raw_read_table(self.sap, table, chosen, options,
                                     rowcount=max_rows + 1, rowskips=skip)
                break
            except ABAPException as exc:
                attempts += 1
                if exc.key == "DATA_BUFFER_EXCEEDED" and len(chosen) > 1 and attempts <= 3:
                    dropped = chosen.pop()
                    notes.append(f"omitted {dropped}: row did not fit the 512-character buffer")
                    continue
                raise ReadTableError(_abap_error_text(table, exc)) from None
            except RFCError as exc:
                raise ReadTableError(f"RFC error reading {table}: {exc.key} {exc.message}") from None

        columns, rows = parse_rows(out.get("DATA", []), out.get("FIELDS", []))
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        texts = {f.name: f.text for f in info["fields"]}
        for c in columns:
            c["text"] = c["text"] or texts.get(c["name"], "")
            c.pop("offset", None)
            c.pop("length", None)
        if truncated:
            notes.append(f"more rows exist; call again with skip={skip + max_rows}")
        result = {
            "table": table,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "skip": skip,
            "truncated": truncated,
            "note": "; ".join(notes),
        }
        self._cache.put(key, result)
        return result


def _abap_error_text(table: str, exc: ABAPException) -> str:
    key = exc.key
    texts = {
        "TABLE_NOT_AVAILABLE": f"table {table} does not exist or is not active",
        "TABLE_WITHOUT_DATA": f"table {table} has no data component",
        "OPTION_NOT_VALID": "the WHERE clause is not valid Open SQL for RFC_READ_TABLE",
        "FIELD_NOT_VALID": "one of the fields is not valid for RFC_READ_TABLE",
        "NOT_AUTHORIZED": f"the RFC user is not authorised to read {table}",
        "DATA_BUFFER_EXCEEDED": "the row is wider than 512 characters; select fewer fields",
    }
    return texts.get(key, f"{key}: {exc.message}".strip())
