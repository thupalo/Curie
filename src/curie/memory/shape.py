"""The shape of an ABAP report and the typed values of a question
(knowledge plan 5.3, 7 P4, 8).

A *shape* is what a program does with the database with its literals
masked: the tables it reads, the fields it filters on and how, the
aggregates, the joins. Two confirmed programs with the same shape answer
the same kind of question with other values; that is a report family.

A *literal* is a value in a WHERE condition (or UP TO n ROWS), bound to
the table field it filters, directly (`gjahr = '2005'`) or through a
variable (`lv_gjahr = '2005'. ... WHERE gjahr = lv_gjahr`).

*Question values* are the typed things a question carries: the
customer's composite document key when one is configured (KEY_PATTERN /
KEY_ROLES, e.g. an agreement written 100/2005 stored as a zero padded
number and a year), a year, a month or a period (month names of the
active languages, curie/lexicon.py), a quoted text, an upper-case code
(a company code), a plain number. A literal that renders one of the
question's values is a parameter of the family; a literal that does not
(cancelled = space, doc_type = 'LEAS') is a constant.

Everything here is deterministic; no model involved. Language-dependent
parts come from curie/lexicon.py, customer conventions from the config.
"""
from __future__ import annotations

import calendar
import hashlib
import logging
import re
from dataclasses import dataclass, field

from curie import config
from curie.memory.store import declared_names, program_tables

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Statements
# --------------------------------------------------------------------------- #

STRING = re.compile(r"'(?:[^']|'')*'")


def statements(source: str) -> list[str]:
    """Code statements without comments, one string each (periods inside
    string literals do not split)."""
    out: list[str] = []
    buf: list[str] = []
    for line in (source or "").split("\n"):
        if line.startswith("*"):
            continue
        code = _strip_inline_comment(line)
        buf.append(code)
        joined = " ".join(buf)
        # a statement ends with a period outside of a string
        masked = STRING.sub(lambda m: "_" * len(m.group(0)), joined)
        while "." in masked:
            pos = masked.index(".")
            if pos + 1 < len(masked) and not masked[pos + 1].isspace():
                # a period inside a name (sy-subrc.) is unusual; treat as end anyway
                pass
            out.append(joined[:pos].strip())
            joined = joined[pos + 1:]
            masked = masked[pos + 1:]
        buf = [joined] if joined.strip() else []
    if buf and buf[0].strip():
        out.append(buf[0].strip())
    return [s for s in out if s]


def _strip_inline_comment(line: str) -> str:
    in_str = False
    for i, ch in enumerate(line):
        if ch == "'":
            in_str = not in_str
        elif ch == '"' and not in_str:
            return line[:i]
    return line


# --------------------------------------------------------------------------- #
# Declarations, assignments, selects
# --------------------------------------------------------------------------- #

LIKE_DECL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+LIKE\s+([A-Za-z_][A-Za-z0-9_/]*)-([A-Za-z_][A-Za-z0-9_]*)",
                       re.IGNORECASE)
ASSIGN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_\-]*)\s*=\s*('(?:[^']|'')*'|\d+(?:\.\d+)?|space)\s*$",
                    re.IGNORECASE)
SELECT_RE = re.compile(r"^\s*SELECT\s+(?P<cols>.*?)\s+FROM\s+(?P<table>[A-Za-z_][A-Za-z0-9_/]*)(?P<rest>.*)$",
                       re.IGNORECASE | re.DOTALL)
# SELECT SINGLE id_zumowyl FROM zumowyl INTO lv_id: the variable now holds
# a value of that table's field (a later WHERE x = lv_id is a join).
INTO_VAR = re.compile(r"^\s*SELECT\s+(?:SINGLE\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+FROM\s+([A-Za-z_][A-Za-z0-9_/]*)"
                      r"\s+INTO\s+(?!TABLE\b)(?!\()([A-Za-z_][A-Za-z0-9_\-]*)", re.IGNORECASE)
UP_TO = re.compile(r"\bUP\s+TO\s+(\d+)\s+ROWS\b", re.IGNORECASE)
AGG = re.compile(r"\b(COUNT|SUM|MAX|MIN|AVG)\s*\(", re.IGNORECASE)
WHERE_SPLIT = re.compile(r"\bWHERE\b", re.IGNORECASE)
VALUE = r"'(?:[^']|'')*'|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_\-]*"
COND = re.compile(
    rf"(?P<field>[A-Za-z_][A-Za-z0-9_\-]*)\s*"
    rf"(?P<op>BETWEEN|LIKE|IN|=|EQ|<>|NE|>=|GE|<=|LE|>|GT|<|LT)\s*"
    rf"(?P<value>{VALUE})",
    re.IGNORECASE)
BETWEEN_END = re.compile(rf"\s+AND\s+(?P<value2>{VALUE})", re.IGNORECASE)


@dataclass
class Literal:
    raw: str            # as written in the program: '2005', 0000100, 30
    value: str          # bare: 2005
    table: str = ""
    field: str = ""
    op: str = "="
    role: str = ""      # "start"/"end" of a BETWEEN or >=/<= pair, "" otherwise
    via: str = ""       # the variable it was assigned to, when indirect

    @property
    def key(self) -> str:
        return f"{self.table}.{self.field}".strip(".")


@dataclass
class Select:
    table: str
    aggregates: list = field(default_factory=list)
    filters: list = field(default_factory=list)     # (field, op)
    up_to: int = 0


@dataclass
class Shape:
    tables: list = field(default_factory=list)
    selects: list = field(default_factory=list)
    joins: list = field(default_factory=list)       # "T1.F1=T2.F2"
    literals: list = field(default_factory=list)
    constants: dict = field(default_factory=dict)   # variable -> literal raw

    @property
    def aggregates(self) -> list[str]:
        return sorted({a for s in self.selects for a in s.aggregates})

    @property
    def filters(self) -> list[str]:
        return sorted({f"{s.table}.{f}:{op}" for s in self.selects for f, op in s.filters})

    def signature(self) -> str:
        """Tables, filters, aggregates and joins with literals masked."""
        text = "|".join([",".join(self.tables), ",".join(self.filters),
                         ",".join(self.aggregates), ",".join(sorted(self.joins))])
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

    def describe(self) -> str:
        parts = [f"tables {', '.join(self.tables) or '-'}"]
        if self.aggregates:
            parts.append("aggregates " + ", ".join(self.aggregates))
        if self.filters:
            parts.append("filters " + ", ".join(self.filters))
        if self.joins:
            parts.append("joins " + ", ".join(sorted(self.joins)))
        return "; ".join(parts)


def analyse(source: str) -> Shape:
    shape = Shape(tables=program_tables(source))
    declared = declared_names(source)
    like: dict[str, tuple[str, str]] = {}          # var or itab-field -> (table, field)
    assigned: dict[str, str] = {}                  # var -> literal raw
    stmts = statements(source)
    for st in stmts:
        for m in LIKE_DECL.finditer(st):
            like[m.group(1).upper()] = (m.group(2).upper(), m.group(3).upper())
        m = ASSIGN.match(st)
        if m:
            assigned[m.group(1).upper()] = m.group(2)
    shape.constants = dict(assigned)
    into: dict[str, tuple[str, str]] = {}          # var -> (table, field) it was selected from
    for st in stmts:
        m = INTO_VAR.match(st)
        if m and m.group(1).upper() not in ("COUNT", "SUM", "MAX", "MIN", "AVG"):
            into[m.group(3).upper()] = (m.group(2).upper(), m.group(1).upper())
    # BEGIN OF lt OCCURS 0, f LIKE t-f: the component is reachable as lt-f
    current_struct = ""
    for st in stmts:
        up = st.upper()
        if "BEGIN OF" in up:
            mm = re.search(r"BEGIN\s+OF\s+([A-Za-z_][A-Za-z0-9_]*)", st, re.IGNORECASE)
            current_struct = mm.group(1).upper() if mm else ""
            for m in LIKE_DECL.finditer(st):
                like[f"{current_struct}-{m.group(1).upper()}"] = (m.group(2).upper(), m.group(3).upper())
        if "END OF" in up:
            current_struct = ""
    for st in stmts:
        m = SELECT_RE.match(st)
        if not m:
            continue
        table = m.group("table").upper()
        if table in declared or table not in shape.tables:
            continue
        sel = Select(table=table)
        sel.aggregates = sorted({a.upper() for a in AGG.findall(m.group("cols"))})
        rest = m.group("rest")
        up = UP_TO.search(rest)
        if up:
            sel.up_to = int(up.group(1))
            shape.literals.append(Literal(raw=up.group(1), value=up.group(1), table=table,
                                          field="", op="UP TO"))
        parts = WHERE_SPLIT.split(rest, maxsplit=1)
        if len(parts) == 2:
            _conditions(parts[1], table, sel, shape, like, assigned, into)
        shape.selects.append(sel)
    shape.joins = sorted(set(shape.joins))
    return shape


def _conditions(text: str, table: str, sel: Select, shape: Shape, like: dict, assigned: dict,
                into: dict | None = None) -> None:
    into = into or {}
    pos = 0
    while True:
        m = COND.search(text, pos)
        if m is None:
            break
        pos = m.end()
        fld = m.group("field").upper()
        if "-" in fld:
            continue                      # itab-field on the left: not a table field
        op = m.group("op").upper()
        op = {"EQ": "=", "NE": "<>", "GE": ">=", "LE": "<=", "GT": ">", "LT": "<"}.get(op, op)
        values = [m.group("value")]
        if op == "BETWEEN":
            m2 = BETWEEN_END.match(text, pos)
            if m2 is None:
                continue
            values.append(m2.group("value2"))
            pos = m2.end()
        sel.filters.append((fld, op))
        for i, val in enumerate(values):
            role = ("start", "end")[i] if op == "BETWEEN" else ("start" if op in (">=", ">") else
                                                                 "end" if op in ("<=", "<") else "")
            _literal(val, table, fld, op, role, shape, like, assigned, into)


def _literal(val: str, table: str, fld: str, op: str, role: str, shape: Shape,
             like: dict, assigned: dict, into: dict | None = None) -> None:
    into = into or {}
    upper = val.upper()
    if upper == "SPACE":
        shape.literals.append(Literal(raw=val, value="", table=table, field=fld, op=op, role=role))
        return
    if val.startswith("'") or val[0].isdigit():
        shape.literals.append(Literal(raw=val, value=val.strip("'"), table=table, field=fld, op=op,
                                      role=role))
        return
    if upper in assigned:                 # a variable holding a literal
        raw = assigned[upper]
        shape.literals.append(Literal(raw=raw, value=raw.strip("'"), table=table, field=fld, op=op,
                                      role=role, via=upper))
        return
    origin = into.get(upper) or like.get(upper)
    if origin is not None:                # a value of another table's field: a join
        if origin != (table, fld):        # a variable typed LIKE the field itself is not a join
            shape.joins.append(f"{table}.{fld}={origin[0]}.{origin[1]}")
        return
    if "-" in upper:
        left = upper.split("-", 1)[0]
        if left in {k.split("-")[0] for k in like} or left.startswith(("LT_", "IT_", "WA")):
            return                        # itab-field without a LIKE: unknown, not a literal
        shape.joins.append(f"{table}.{fld}={upper.replace('-', '.')}")


# --------------------------------------------------------------------------- #
# Typed values in questions
# --------------------------------------------------------------------------- #

# Language-dependent: month names, the suffix a year may carry and the
# accent folding come from curie/lexicon.py for the active languages.
# Customer conventions: the composite document key (KEY_PATTERN /
# KEY_ROLES) and the codes to ignore (VALUE_STOP_CODES) come from config.
MONTHS: dict[str, int] = {}
_FOLD: dict = {}
MONTH_WORD = r"[^\W\d_]{3,13}"
PERIOD = re.compile(rf"\b({MONTH_WORD})\s*[-–]\s*({MONTH_WORD})\s+(\d{{4}})(?!\d)")
MONTH = re.compile(rf"\b({MONTH_WORD})\s+(\d{{4}})(?!\d)")
YEAR = re.compile(r"(?<![\d/])((?:19|20)\d{2})(?![\d/])")
QUOTED = re.compile(r"'([^']+)'|\"([^\"]+)\"")
CODE = re.compile(r"\b([A-Z][A-Z0-9]{2,5})\b")
NUMBER = re.compile(r"(?<![\d/.,])(\d+(?:[.,]\d+)?)(?![\d/])")
# SAP and ABAP words, module and currency codes: never values.
GENERIC_STOP_CODES = {"SAP", "ABAP", "FI", "SD", "MM", "CO", "HR", "PP", "PLN", "EUR", "USD", "GBP", "CHF",
                      "VIN", "NIP", "OK", "SELECT", "COUNT", "SUM", "MAX", "MIN", "AVG", "ROWS", "GUI",
                      "II", "III", "IV"}
STOP_CODES: set[str] = set(GENERIC_STOP_CODES)
KEY: re.Pattern | None = None
KEY_ROLES: list[tuple[str, int]] = []
TYPE_ALIASES = {"agreement": "key"}          # notes written before the key type was configurable


def configure(pattern: str | None = None, roles: str | None = None, stop_codes=None) -> None:
    """(Re)build the language and convention tables from the config, or
    from explicit values (tests). Called at import and by the CLI after
    the configuration is final."""
    global MONTHS, _FOLD, YEAR, STOP_CODES, KEY, KEY_ROLES
    from curie import lexicon

    MONTHS = lexicon.merged_dict("months")
    _FOLD = lexicon.transliteration()
    suffix = lexicon.year_suffix()
    YEAR = re.compile(r"(?<![\d/])((?:19|20)\d{2})" + (f"(?:{suffix})?" if suffix else "") + r"(?![\d/])")
    STOP_CODES = set(GENERIC_STOP_CODES) | {c.upper() for c in (
        config.VALUE_STOP_CODES if stop_codes is None else stop_codes)}
    pattern = config.KEY_PATTERN if pattern is None else pattern
    try:
        KEY = re.compile(pattern) if pattern else None
    except re.error as exc:
        log.warning("KEY_PATTERN %r is not a valid regex: %s", pattern, exc)
        KEY = None
    KEY_ROLES = config.key_roles() if roles is None else _parse_roles(roles)
    if KEY is not None and KEY.groups != len(KEY_ROLES):
        log.warning("KEY_PATTERN has %d group(s) but KEY_ROLES names %d part(s); the key is off",
                    KEY.groups, len(KEY_ROLES))
        KEY = None


def _parse_roles(roles: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for item in (roles or "").split(","):
        item = item.strip()
        if item:
            name, _, width = item.partition(":")
            out.append((name.strip().lower(), int(width) if width.strip().isdigit() else 0))
    return out


def value_type(name: str) -> str:
    """The current name of a value type (older notes say "agreement")."""
    return TYPE_ALIASES.get(name, name)


@dataclass
class Value:
    type: str           # agreement | year | month | period | quoted | code | number
    text: str           # as written
    renderings: dict = field(default_factory=dict)   # role -> literal value
    start: int = 0
    end: int = 0

    def renders(self, literal_value: str) -> str | None:
        """The role under which this value renders `literal_value`, or None.
        A zero-padded literal ('0000125812') also takes the bare number."""
        for role, rendered in self.renderings.items():
            if rendered == literal_value:
                return role
        if literal_value.startswith("0") and literal_value.isdigit():
            for role, rendered in self.renderings.items():
                if rendered.isdigit() and len(rendered) < len(literal_value) \
                        and rendered.lstrip("0") == literal_value.lstrip("0"):
                    return role
        return None

    @property
    def is_range(self) -> bool:
        return "start" in self.renderings and "end" in self.renderings


def question_values(question: str) -> list[Value]:
    """Typed values of a question, in order of appearance; overlapping
    matches keep the most specific (agreement over year over number)."""
    text = question or ""
    found: list[Value] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    def add(v: Value) -> None:
        if free(v.start, v.end):
            found.append(v)
            taken.append((v.start, v.end))

    if KEY is not None:
        for m in KEY.finditer(text):
            renderings = {"text": m.group(0)}
            for (role, width), part in zip(KEY_ROLES, m.groups()):
                part = part or ""
                renderings[role] = part.zfill(width) if width and part.isdigit() else part
            add(Value("key", m.group(0), renderings, m.start(), m.end()))
    for m in PERIOD.finditer(text):
        a, b = _month(m.group(1)), _month(m.group(2))
        if a and b:
            year = int(m.group(3))
            add(Value("period", m.group(0), {"start": f"{year}{a:02d}01", "end": _month_end(year, b),
                                             "year": str(year)}, m.start(), m.end()))
    for m in MONTH.finditer(text):
        mo = _month(m.group(1))
        if mo:
            year = int(m.group(2))
            add(Value("month", m.group(0), {"start": f"{year}{mo:02d}01", "end": _month_end(year, mo),
                                            "year": str(year), "month": f"{mo:02d}"}, m.start(), m.end()))
    for m in YEAR.finditer(text):
        add(Value("year", m.group(1), {"year": m.group(1), "start": m.group(1) + "0101",
                                       "end": m.group(1) + "1231"}, m.start(), m.end()))
    for m in QUOTED.finditer(text):
        val = m.group(1) or m.group(2)
        add(Value("quoted", val, {"text": val, "upper": val.upper(), "prefix": val.upper() + "%"},
                  m.start(), m.end()))
    for m in CODE.finditer(text):
        if m.group(1) in STOP_CODES or m.group(1).isdigit():
            continue
        add(Value("code", m.group(1), {"code": m.group(1), "prefix": m.group(1) + "%"},
                  m.start(), m.end()))
    for m in NUMBER.finditer(text):
        raw = m.group(1).replace(",", ".")
        add(Value("number", m.group(1), {"number": raw, "padded10": raw.zfill(10) if raw.isdigit() else raw,
                                         "padded7": raw.zfill(7) if raw.isdigit() else raw},
                  m.start(), m.end()))
    found.sort(key=lambda v: v.start)
    return found


def _month(word: str) -> int:
    return MONTHS.get(word.lower().translate(_FOLD), 0)


def _month_end(year: int, month: int) -> str:
    return f"{year}{month:02d}{calendar.monthrange(year, month)[1]:02d}"


def masked_question(question: str) -> str:
    """The question with its typed values replaced by their type names:
    the wording that identifies a family's intent."""
    text = question or ""
    for v in sorted(question_values(text), key=lambda v: -v.start):
        text = text[:v.start] + "{" + v.type + "}" + text[v.end:]
    return " ".join(text.split())


# --------------------------------------------------------------------------- #
# Binding a question's values to a program's literals
# --------------------------------------------------------------------------- #


@dataclass
class Param:
    name: str
    type: str           # the value type
    role: str           # which rendering of the value fills the literal
    table: str
    field: str
    op: str
    literal: str        # the raw literal in the canonical program
    example: str        # the value as the canonical question wrote it

    def as_dict(self) -> dict:
        return {"type": self.type, "role": self.role, "field": self.key, "op": self.op,
                "literal": self.literal, "example": self.example}

    @property
    def key(self) -> str:
        return f"{self.table}.{self.field}".strip(".")


def bind(question: str, shape: Shape) -> list[Param]:
    """The literals of the shape that render a value of the question, as
    named parameters; the rest are the family's constants."""
    values = question_values(question)
    params: list[Param] = []
    seen: set[tuple[str, str]] = set()
    for lit in shape.literals:
        if not lit.value:
            continue
        for v in values:
            if lit.op == "UP TO":
                # a row limit is a parameter only for a plain number the
                # question states as such ("pierwsze 30 umów")
                if v.type != "number" or v.text != lit.value:
                    continue
                role = "number"
            else:
                role = v.renders(lit.value)
            if role is None:
                continue
            ptype = "range" if role in ("start", "end") and v.is_range else v.type
            key = (lit.raw, role)
            if key in seen:               # the same literal in another SELECT: one parameter
                break
            seen.add(key)
            name = _param_name(v, role, lit, [p.name for p in params])
            params.append(Param(name=name, type=ptype, role=role, table=lit.table, field=lit.field,
                                op=lit.op, literal=lit.raw, example=v.text))
            break
    return params


def _param_name(v: Value, role: str, lit: Literal, taken: list[str]) -> str:
    base = (lit.field or "rows").lower()
    if v.type == "key":
        base = role                       # the part's name from KEY_ROLES
    elif role in ("start", "end"):
        base = f"{base}_{role}"
    name = base
    i = 2
    while name in taken:
        name = f"{base}_{i}"
        i += 1
    return name


def fill(template: str, params: dict, values: list[Value]) -> tuple[str, dict] | None:
    """Substitute the question's values into a family template.

    `params` is {name: {"type", "role", "literal", ...}}; every parameter
    needs a value of its type (one value can fill several parameters, a
    composite key fills each of its parts). None when a parameter has no
    value."""
    program = template
    filled: dict[str, str] = {}
    by_type: dict[str, list[Value]] = {}
    for v in values:
        by_type.setdefault(v.type, []).append(v)
        if v.is_range:                    # a year, a month or a period fills a range
            by_type.setdefault("range", []).append(v)
    # Parameters that share an example value (the parts of one key, start
    # + end of one month) must take the same question value.
    chosen: dict[str, Value] = {}
    for name, spec in params.items():
        vtype = value_type(spec["type"])
        candidates = by_type.get(vtype) or []
        if not candidates:
            return None
        key = f"{vtype}:{spec.get('example', '')}"
        v = chosen.get(key)
        if v is None:
            unused = [c for c in candidates if c not in chosen.values()]
            v = (unused or candidates)[0]
            chosen[key] = v
        rendered = v.renderings.get(spec["role"])
        if rendered is None:
            return None
        literal = str(spec["literal"])
        if literal.startswith("'"):
            new = "'" + rendered + "'"
        else:
            new = rendered
        filled[name] = rendered
        program = program.replace("{{" + name + "}}", new)
        program = program.replace("{{" + name + ":bare}}", rendered)
        program = program.replace("{{" + name + ":text}}", v.text)
    if "{{" in program:
        return None
    return program, filled


def leaked_values(params: dict, program: str, values: list[Value]) -> list[str]:
    """Texts of the template's canonical values that are still inside
    string literals of a filled program although the new question does
    not carry them: a template that would print another question's
    values ("umowa 339/2005" for a question about 28/2005)."""
    wanted: set[str] = set()
    for v in values:
        wanted.add(v.text)
        wanted.update(str(r) for r in v.renderings.values())
    candidates: set[str] = set()
    for spec in params.values():
        example = str(spec.get("example") or "").strip()
        if len(example) >= 4:
            candidates.add(example)
        literal = str(spec.get("literal") or "").strip("'")
        if len(literal) >= 4:
            candidates.add(literal)
    strings = " ".join(m.group(0) for m in STRING.finditer(program or ""))
    return sorted(c for c in candidates if c not in wanted and c in strings)


OUTPUT_STATEMENTS = ("WRITE", "ULINE", "SKIP", "FORMAT", "NEW-LINE", "NEW-PAGE", "REPORT", "PROGRAM")


def skeleton(program: str, params: list | None = None) -> list[str]:
    """The program's logic as comparable statements: output statements and
    comments dropped, declared names masked as V, literals as L, parameter
    holes kept; what makes two programs variants of one report."""
    names = declared_names(program)
    text = templatize(program, params or [])
    out = []
    for st in statements(text):
        head = st.split(None, 1)[0].upper().rstrip(":") if st.split() else ""
        if head in OUTPUT_STATEMENTS:
            continue
        st = STRING.sub("L", st)
        st = re.sub(r"(?<![\w{])\d+(?:\.\d+)?(?![\w}])", "L", st)
        for name in names:
            st = re.sub(rf"\b{re.escape(name)}\b", "V", st, flags=re.IGNORECASE)
        out.append(" ".join(st.upper().split()))
    return out


def similarity(a: list[str], b: list[str]) -> float:
    """0..1: how alike two skeletons are (difflib on the statement lists)."""
    import difflib
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio()


def templatize(program: str, params: list[Param]) -> str:
    """The program with each parameter's literal replaced by {{name}}; the
    literal's every occurrence (a variable assignment and a WRITE) goes."""
    out = program
    # The value as the question wrote it ("339/2005") inside a text
    # ('umowa 339/2005'): one hole for the whole written form, filled with
    # the new question's wording, before the parts are looked at (the
    # parts alone would leave "339/" behind).
    seen_examples: set[str] = set()
    for p in params:
        example = (p.example or "").strip()
        # Only a composite key has a written form of its own; a year or a
        # number written as such is the literal itself.
        if value_type(p.type) != "key" or len(example) < 4 or example in seen_examples \
                or example == p.literal.strip("'"):
            continue
        seen_examples.add(example)

        def in_string_text(m, _ex=example, _name=p.name):
            return m.group(0).replace(_ex, "{{" + _name + ":text}}")
        out = STRING.sub(in_string_text, out)
    for p in params:
        bare = p.literal.strip("'")
        if p.literal.startswith("'"):
            out = out.replace(p.literal, "{{" + p.name + "}}")
        else:
            out = re.sub(rf"(?<![\w.']){re.escape(p.literal)}(?![\w.'])", "{{" + p.name + "}}", out)
        # The value inside a longer text ('Liczba umow w roku 2005:'): a
        # replay for another year must not print 2005. A bare hole, filled
        # without quotes; only values long enough not to be a coincidence.
        if len(bare) >= 4:
            def in_string(m, _bare=bare, _name=p.name):
                return m.group(0).replace(_bare, "{{" + _name + ":bare}}")
            out = STRING.sub(in_string, out)
    return out


configure()
