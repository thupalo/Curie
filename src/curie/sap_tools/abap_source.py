"""Turning the model's ```abap block into source SAP will accept.

* extract_abap_blocks: the fenced blocks of an answer.
* prepare_source: strip the model's REPORT line, inject ours, normalise
  whitespace, and fold lines to the release's width (72 on 4.5B) without
  breaking string literals or turning comment text into code.
* encoding_problems: characters the backend codepage cannot carry.
* repair_source: the deterministic repairs the archive justified (a
  period missing before ELSE/ENDIF, an indented `*` comment, UP TO n ROWS
  after WHERE), applied before the syntax check instead of a model round
  trip.
* dialect_problems: 7.40+ constructs the release cannot compile, named
  before SAP is asked.
* new_progname: ZAI_ + 8 hex, the name the backend sees.
"""
from __future__ import annotations

import re
import secrets

from curie import config

FENCE = re.compile(r"```(?:abap|ABAP|Abap)\s*\n(.*?)```", re.DOTALL)
REPORT_LINE = re.compile(r"^\s*(REPORT|PROGRAM)\b[^.]*\.\s*", re.IGNORECASE | re.DOTALL)


def extract_abap_blocks(text: str) -> list[str]:
    return [m.group(1).strip("\n") for m in FENCE.finditer(text or "")]


def new_progname(prefix: str | None = None) -> str:
    return f"{(prefix or config.ABAP_PROG_PREFIX).upper()}{secrets.token_hex(4).upper()}"


# --------------------------------------------------------------------------- #
# Line folding
# --------------------------------------------------------------------------- #


def _split_comment(line: str) -> tuple[str, str]:
    """Code part and inline comment part (a " outside quotes starts one)."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", "`"):
            quote = ch
        elif ch == '"':
            return line[:i].rstrip(), line[i:]
    return line, ""


def _break_positions(code: str) -> list[int]:
    """Indexes of spaces outside quotes where the line may be broken."""
    out, quote = [], None
    for i, ch in enumerate(code):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", "`"):
            quote = ch
        elif ch == " ":
            out.append(i)
    return out


def fold_line(line: str, width: int) -> list[str]:
    """Fold one source line to `width`; raises ValueError when impossible."""
    line = line.rstrip()
    if len(line) <= width:
        return [line]
    if line.startswith("*"):
        text = line[1:].strip()
        out = []
        while text:
            chunk = text[:width - 2]
            cut = chunk.rfind(" ")
            if len(text) > width - 2 and cut > 0:
                chunk = text[:cut]
            out.append("* " + chunk.strip())
            text = text[len(chunk):].strip()
        return out
    code, comment = _split_comment(line)
    out: list[str] = []
    indent = len(code) - len(code.lstrip(" "))
    cont_indent = " " * min(indent + 4, width // 2)
    while len(code) > width:
        lead = len(code) - len(code.lstrip(" "))
        # Only breaks after the leading indent make the line shorter.
        breaks = [b for b in _break_positions(code) if lead < b <= width]
        if not breaks:
            raise ValueError(f"cannot fold line to {width} characters: {line[:60]}")
        cut = breaks[-1]
        head = code[:cut].rstrip()
        rest = cont_indent + code[cut:].lstrip()
        if not head.strip() or len(rest) >= len(code):
            raise ValueError(f"cannot fold line to {width} characters: {line[:60]}")
        out.append(head)
        code = rest
    if code.strip():
        out.append(code)
    if comment:
        text = comment[1:].strip()
        while text:
            chunk = text[:width - 2]
            cut = chunk.rfind(" ")
            if len(text) > width - 2 and cut > 0:
                chunk = text[:cut]
            out.append('" ' + chunk.strip())
            text = text[len(chunk):].strip()
    return out


def prepare_source(code: str, progname: str, width: int | None = None) -> tuple[list[str], list[str]]:
    """Source lines for SAP, and the problems found (empty when fine)."""
    width = width or config.profile()["line_width"]
    problems: list[str] = []
    text = (code or "").replace("\r\n", "\n").replace("\t", "  ")
    text = REPORT_LINE.sub("", text, count=1)
    lines = [f"REPORT {progname.lower()}."]
    for raw in text.split("\n"):
        raw = raw.rstrip()
        try:
            lines.extend(fold_line(raw, width))
        except ValueError as exc:
            problems.append(str(exc))
            lines.append(raw)
    while len(lines) > 1 and not lines[-1].strip():
        lines.pop()
    if len(lines) < 2:
        problems.append("the program has no statements")
    return lines, problems


# Typographic characters models like to write, and their ASCII stand-ins.
TRANSLITERATE = str.maketrans({
    "–": "-", "—": "-", "‒": "-", "−": "-", "‐": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "…": "...", " ": " ", " ": " ", " ": " ",
    "→": "->", "←": "<-", "•": "*", "×": "x",
    "≤": "<=", "≥": ">=", "≠": "<>", "﻿": "",
})


def sanitize_for_codepage(lines: list[str], codepage: str | None = None) -> tuple[list[str], list[str]]:
    """Make the source representable in the backend codepage.

    Typographic dashes, quotes and the like become their ASCII forms; any
    character still outside the codepage becomes '?'. Returns the new lines
    and a note per changed line, so the user can see what happened without
    the model being sent on a fix round trip for a dash in a comment.
    """
    codepage = codepage or config.profile()["codepage"]
    out, notes = [], []
    for n, line in enumerate(lines, 1):
        fixed = line.translate(TRANSLITERATE)
        try:
            fixed.encode(codepage)
        except UnicodeEncodeError:
            fixed = fixed.encode(codepage, errors="replace").decode(codepage)
        if fixed != line:
            lost = sorted({c for c in line if c not in fixed and ord(c) > 127})
            notes.append(f"line {n}: replaced {' '.join(repr(c) for c in lost)}")
        out.append(fixed)
    return out, notes


def encoding_problems(lines: list[str], codepage: str | None = None) -> list[str]:
    codepage = codepage or config.profile()["codepage"]
    bad = []
    for n, line in enumerate(lines, 1):
        try:
            line.encode(codepage)
        except UnicodeEncodeError as exc:
            bad.append(f"line {n}: character {line[exc.start]!r} cannot be "
                       f"represented in the SAP codepage ({codepage})")
    return bad


def strip_comments(lines: list[str]) -> list[str]:
    """Code without comments, for the static rules."""
    out = []
    for line in lines:
        if line.startswith("*"):
            out.append("")
            continue
        code, _ = _split_comment(line)
        out.append(code)
    return out


# --------------------------------------------------------------------------- #
# Deterministic repairs (P11): what the archive showed the model gets wrong
# again and again, and what a person would fix without thinking.
# --------------------------------------------------------------------------- #

# Keywords that begin a statement which closes or continues a block: the
# statement before them must have ended with a period.
BLOCK_KEYWORDS = ("ELSEIF", "ELSE", "ENDIF", "ENDLOOP", "ENDSELECT", "ENDDO", "ENDWHILE", "ENDCASE",
                  "ENDTRY", "ENDFORM", "ENDAT", "ENDFUNCTION", "ENDMODULE", "WHEN", "CATCH")
# A statement whose last token is one of these is not complete: no period
# can be added blindly.
OPEN_ENDINGS = ("AND", "OR", "NOT", "=", "<", ">", "<=", ">=", "<>", "(", "+", "-", "*", "/", ":", "INTO",
                "FROM", "WHERE", "TYPE", "LIKE", "OF", "AT", "TO", "IN", "BY", "WITH", "FOR")
BLOCK_START = re.compile(r"^\s*(" + "|".join(BLOCK_KEYWORDS) + r")\b", re.IGNORECASE)
INDENTED_STAR = re.compile(r"^(\s+)\*(.*)$")
UP_TO_CLAUSE = re.compile(r"\s*\bUP\s+TO\s+(\d+)\s+ROWS\b", re.IGNORECASE)
SELECT_START = re.compile(r"^\s*SELECT\b(?!\s+SINGLE\b)", re.IGNORECASE)
WHERE_OR_LATER = re.compile(r"\b(WHERE|GROUP\s+BY|ORDER\s+BY|HAVING)\b", re.IGNORECASE)
SORT_OR_SELECT = re.compile(r"^\s*(SELECT|SORT)\b", re.IGNORECASE)
SQL_DIRECTION = re.compile(r"\b(DESC|ASC)\b(?=\s|\.|,|$)", re.IGNORECASE)


def _sub_outside_literals(line: str, pattern: re.Pattern, repl) -> str:
    """Regex substitution on the code part of a line, skipping string
    literals and the inline comment."""
    code, comment = _split_comment(line)
    out, quote, start = [], None, 0
    for i, ch in enumerate(code):
        if quote:
            if ch == quote:
                quote = None
                out.append(code[start:i + 1])
                start = i + 1
        elif ch in ("'", "`"):
            out.append(pattern.sub(repl, code[start:i]))
            quote = ch
            start = i
    out.append(pattern.sub(repl, code[start:]) if quote is None else code[start:])
    return "".join(out) + ((" " + comment) if comment else "")


def _code_only(line: str) -> str:
    """The code part of a line with literal contents blanked: comments
    dropped, quotes kept so periods and keywords inside strings do not
    count."""
    if line.startswith("*"):
        return ""
    code, _ = _split_comment(line)
    out, quote = [], None
    for ch in code:
        if quote:
            out.append(quote if ch == quote else " ")
            if ch == quote:
                quote = None
        elif ch in ("'", "`"):
            quote = ch
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def _statement_open(lines: list[str], upto: int) -> int | None:
    """Index of the last code line before `upto` when the statement it
    belongs to has not been closed with a period; None when the previous
    statement is complete (or there is none)."""
    for i in range(upto - 1, -1, -1):
        code = _code_only(lines[i]).strip()
        if not code:
            continue
        return None if code.endswith(".") else i
    return None


def _ends_open(code: str) -> bool:
    last = code.rstrip().rstrip(",").split()
    return bool(last) and last[-1].upper() in OPEN_ENDINGS


def repair_source(lines: list[str]) -> tuple[list[str], list[str]]:
    """Apply the deterministic repairs; returns the new lines and one note
    per change ("line 12: period added before ENDIF"). Every repair is
    conservative: the archive showed the same syntax error each time and
    the same one-token fix each time; anything less clear is left to SAP."""
    out = list(lines)
    notes: list[str] = []

    # 1. `*` comment lines the model indented: SAP reads them as a
    #    statement "*". Only when no statement is open (a `*` continuing an
    #    arithmetic expression must stay).
    for i, line in enumerate(out):
        m = INDENTED_STAR.match(line)
        if m and _statement_open(out, i) is None:
            text = m.group(2).strip()
            out[i] = "* " + text if text else "*"
            notes.append(f"line {i + 1}: comment marker moved to column 1")

    # 2. A period missing before ELSE / ENDIF / ENDLOOP ...: the previous
    #    statement gets it, unless it visibly continues (ends with an
    #    operator or a keyword that needs an operand).
    for i, line in enumerate(out):
        bm = BLOCK_START.match(line)
        if not bm:
            continue
        prev = _statement_open(out, i)
        if prev is None:
            continue
        code, comment = _split_comment(out[prev])
        stripped = code.rstrip()
        if stripped.endswith(","):
            fixed = stripped[:-1].rstrip() + "."
        elif _ends_open(stripped):
            continue
        else:
            fixed = stripped + "."
        out[prev] = fixed + (" " + comment if comment else "")
        notes.append(f"line {prev + 1}: period added before {bm.group(1).upper()}")

    # 3. UP TO n ROWS after WHERE (or GROUP BY / ORDER BY): before 7.40 the
    #    clause belongs before WHERE; SAP reports 'Incorrect expression "UP"'.
    i = 0
    while i < len(out):
        if not SELECT_START.match(_code_only(out[i])):
            i += 1
            continue
        end = i
        while end < len(out) - 1 and not _code_only(out[end]).rstrip().endswith("."):
            end += 1
        where_at = None
        for j in range(i, end + 1):
            if WHERE_OR_LATER.search(_code_only(out[j])):
                where_at = j
                break
        if where_at is None:
            i = end + 1
            continue
        for j in range(where_at, end + 1):
            code, comment = _split_comment(out[j])
            blanked = _code_only(out[j])
            m = UP_TO_CLAUSE.search(blanked)
            if not m:
                continue
            wm = WHERE_OR_LATER.search(blanked)
            if j == where_at and wm and m.start() < wm.start():
                break                      # already before WHERE on the same line
            rest = (code[:m.start()] + code[m.end():]).rstrip()
            indent = " " * (len(out[where_at]) - len(out[where_at].lstrip(" ")))
            clause = f"{indent}UP TO {m.group(1)} ROWS"
            if j == where_at and wm:
                # Clause and WHERE on one line: the clause moves in front
                # of the keyword on that line.
                out[j] = (rest[:wm.start()] + f"UP TO {m.group(1)} ROWS " + rest[wm.start():]
                          + (" " + comment if comment else ""))
                notes.append(f"line {j + 1}: UP TO {m.group(1)} ROWS moved before WHERE")
                break
            if rest.strip() in ("", "."):
                # The clause had the line to itself: the period, if any,
                # moves to the previous code line.
                if rest.strip() == ".":
                    prev = next(k for k in range(j - 1, i - 1, -1) if _code_only(out[k]).strip())
                    pcode, pcomment = _split_comment(out[prev])
                    out[prev] = pcode.rstrip() + "." + (" " + pcomment if pcomment else "")
                del out[j]
                end -= 1
            else:
                out[j] = rest + (" " + comment if comment else "")
            out.insert(where_at, clause)
            end += 1
            notes.append(f"line {where_at + 1}: UP TO {m.group(1)} ROWS moved before WHERE")
            break
        i = end + 1

    # 4. DESC / ASC in ORDER BY or SORT ... BY: ABAP spells them
    #    DESCENDING / ASCENDING ('Unknown column name "DESC"').
    i = 0
    while i < len(out):
        if not SORT_OR_SELECT.match(_code_only(out[i])):
            i += 1
            continue
        end = i
        while end < len(out) - 1 and not _code_only(out[end]).rstrip().endswith("."):
            end += 1
        for j in range(i, end + 1):
            if SQL_DIRECTION.search(_code_only(out[j])):
                fixed = _sub_outside_literals(
                    out[j], SQL_DIRECTION,
                    lambda m: "DESCENDING" if m.group(1).upper() == "DESC" else "ASCENDING")
                if fixed != out[j]:
                    out[j] = fixed
                    notes.append(f"line {j + 1}: DESC/ASC written as DESCENDING/ASCENDING")
        i = end + 1
    return out, notes


# 7.40+ constructs a 4.5B system cannot compile; naming them costs no SAP
# round trip and gives the model the reason instead of a bare error.
DIALECT_45B = [
    (re.compile(r"\bDATA\s*\(", re.IGNORECASE), "inline DATA(...) declarations",
     "declare with DATA before use"),
    (re.compile(r"\bFIELD-SYMBOL\s*\(", re.IGNORECASE), "inline FIELD-SYMBOL(...)",
     "FIELD-SYMBOLS <fs> TYPE ..."),
    (re.compile(r"(?<![\w'])@\w"), "host variables with @", "plain variable names in SELECT"),
    (re.compile(r"\b(VALUE|NEW|CONV|COND|SWITCH|REDUCE|CORRESPONDING|EXACT|REF)\s+#?\s*\(", re.IGNORECASE),
     "constructor expressions (VALUE #, CONV, COND, ...)", "MOVE, APPEND and IF statements"),
    (re.compile(r"\|"), "string templates |...|", "WRITE with several fields, or CONCATENATE"),
    (re.compile(r"->|=>"), "object references (->, =>)", "function modules and FORM routines"),
    (re.compile(r"\b(LINE_EXISTS|LINE_INDEX|XSDBOOL)\s*\(", re.IGNORECASE),
     "table functions (line_exists, xsdbool)", "READ TABLE and sy-subrc"),
]


def dialect_problems(lines: list[str], release: str | None = None) -> list[str]:
    """Constructs the release cannot compile, one problem per line; only the
    4.5B profile has them."""
    release = release or config.SAP_RELEASE or "45B"
    if release != "45B":
        return []
    problems: list[str] = []
    for n, line in enumerate(lines, 1):
        code = _code_only(line)
        if not code.strip():
            continue
        for pattern, what, instead in DIALECT_45B:
            if pattern.search(code):
                problems.append(f"line {n}: {what} are not available on release 4.5B; use {instead}")
                break
    return problems
