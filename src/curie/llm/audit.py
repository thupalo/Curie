"""Two gates before generated ABAP may run: deterministic rules here in
Python, and an independent model review with a strict prompt.

The static rules work on the source with comments removed and string
literals blanked, split into statements at periods. They are deliberately
conservative; a false positive costs one fix round trip, a false negative
costs more.
"""
from __future__ import annotations

import fnmatch
import logging
import re

from curie import config
from curie.llm.client import LLMError, parse_json_object
from curie.llm.prompts import auditor_prompt
from curie.sap_tools.abap_source import strip_comments

log = logging.getLogger(__name__)

# (regex on one upper-cased statement, message)
FORBIDDEN = [
    (r"\bCOMMIT\s+WORK\b", "COMMIT WORK is not allowed"),
    (r"\bROLLBACK\s+WORK\b", "ROLLBACK WORK is not allowed"),
    (r"\bEXEC\s+SQL\b", "native SQL (EXEC SQL) is not allowed"),
    (r"\bCALL\s+'", "kernel calls (CALL 'name') are not allowed"),
    (r"\bSYSTEM-CALL\b", "SYSTEM-CALL is not allowed"),
    (r"\bINSERT\s+REPORT\b", "INSERT REPORT is not allowed"),
    (r"\bDELETE\s+REPORT\b", "DELETE REPORT is not allowed"),
    (r"\bGENERATE\s+(SUBROUTINE\s+POOL|REPORT|DYNPRO)\b", "dynamic program generation is not allowed"),
    (r"\bCALL\s+TRANSACTION\b", "CALL TRANSACTION is not allowed"),
    (r"\bLEAVE\s+TO\s+TRANSACTION\b", "LEAVE TO TRANSACTION is not allowed"),
    (r"\bSUBMIT\b", "SUBMIT is not allowed"),
    (r"\b(OPEN|DELETE|TRANSFER|TRUNCATE)\s+DATASET\b", "file access (DATASET) is not allowed"),
    (r"\bCALL\s+FUNCTION\s+(?:'[^']*'|\S+)\s+DESTINATION\b", "remote function calls (DESTINATION) are not allowed"),
    (r"\bSTARTING\s+NEW\s+TASK\b", "asynchronous RFC (STARTING NEW TASK) is not allowed"),
    (r"\bIN\s+UPDATE\s+TASK\b", "update task calls are not allowed"),
    (r"\bIN\s+BACKGROUND\s+TASK\b", "background task calls are not allowed"),
    (r"\bSET\s+PF-STATUS\b", "PF-STATUS (dialog) is not allowed"),
    (r"\bCALL\s+(SCREEN|DIALOG|SELECTION-SCREEN)\b", "screens and dialogs are not allowed"),
    (r"\bEDITOR-CALL\b", "EDITOR-CALL is not allowed"),
    (r"\bCALL\s+METHOD\s+OF\b", "OLE automation is not allowed"),
    (r"\bEXPORT\b.*\bTO\s+(DATABASE|SHARED\s+BUFFER|SHARED\s+MEMORY)\b", "EXPORT TO DATABASE/SHARED is not allowed"),
    (r"\bDELETE\s+FROM\s+(DATABASE|SHARED\s+BUFFER|SHARED\s+MEMORY)\b", "DELETE FROM DATABASE is not allowed"),
    (r"\b(PARAMETERS|PARAMETER|SELECT-OPTIONS|SELECTION-SCREEN)\b", "selection screens are not allowed; use constants"),
    (r"\bMESSAGE\s+[EAX]\d{3}\b", "MESSAGE of type E/A/X aborts the job; use WRITE"),
    (r"\bMESSAGE\b.*\bTYPE\s+'[EAX]'", "MESSAGE of type E/A/X aborts the job; use WRITE"),
    (r"\bUPDATE\s+\(?\w+", "UPDATE on a database table is not allowed"),
    (r"\bDELETE\s+FROM\s+\(?\w+", "DELETE FROM a database table is not allowed"),
    (r"\bINSERT\s+INTO\s+(?!TABLE\b)\(?\w+", "INSERT INTO a database table is not allowed"),
    (r"\bINSERT\s+\(?\w+\)?\s+FROM\s+(TABLE\s+)?\w+", "INSERT dbtab FROM is not allowed"),
    (r"\bINSERT\s+\(?\w+\)?\s+CLIENT\s+SPECIFIED\b", "INSERT on a database table is not allowed"),
    (r"\bMODIFY\s+\(?\w+\)?\s+CLIENT\s+SPECIFIED\b", "MODIFY on a database table is not allowed"),
    (r"\bDELETE\s+\(?\w+\)?\s+CLIENT\s+SPECIFIED\b", "DELETE on a database table is not allowed"),
]

# Function module policy lives in config (ABAP_FUNCTION_POLICY / _DENY /
# _ALLOW in .env); config.function_allowed() applies it.

ITAB_DECL = [
    re.compile(r"\b(\w+)\s+(?:TYPE|LIKE)\s+(?:STANDARD\s+|SORTED\s+|HASHED\s+)?TABLE\s+OF\b"),
    re.compile(r"\b(\w+)(?:\(\d+\))?\s+(?:TYPE|LIKE)\s+\S+\s+OCCURS\b"),
    re.compile(r"\b(\w+)\s+OCCURS\b"),
    re.compile(r"\bRANGES\s+(\w+)\b"),
    re.compile(r"\b(\w+)\s+TYPE\s+RANGE\s+OF\b"),
]
ITAB_TARGET = [
    (re.compile(r"\bMODIFY\s+(?!SCREEN\b|LINE\b|CURRENT\b)(\w+)\b"), "MODIFY on a database table is not allowed"),
    (re.compile(r"\bDELETE\s+(?!FROM\b|TABLE\b|ADJACENT\b|DATASET\b|REPORT\b|TEXTPOOL\b|DYNPRO\b|CLUSTER\b)(\w+)\b"), "DELETE on a database table is not allowed"),
]

SELECT_RE = re.compile(r"^\s*SELECT\b(?!\s+SINGLE\b)")
UP_TO_RE = re.compile(r"\bUP\s+TO\s+(\d+)\s+ROWS\b")
FROM_RE = re.compile(r"\b(?:FROM|JOIN)\s+\(?(\w+)")
CALL_FUNCTION_RE = re.compile(r"\bCALL\s+FUNCTION\s+'([^']+)'")


def _blank_literals(line: str) -> str:
    out, quote = [], None
    for ch in line:
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


def statements(lines: list[str]) -> list[tuple[int, str, str]]:
    """(first line number, upper-cased statement without literals, original)."""
    code = strip_comments(lines)
    out = []
    buf: list[str] = []
    raw: list[str] = []
    start = 1
    for n, line in enumerate(code, 1):
        blanked = _blank_literals(line)
        pos = 0
        while True:
            idx = blanked.find(".", pos)
            if idx < 0:
                if not buf:
                    start = n
                if blanked[pos:].strip():
                    if not buf:
                        start = n
                    buf.append(blanked[pos:])
                    raw.append(lines[n - 1])
                break
            if not buf and blanked[pos:idx].strip():
                start = n
            buf.append(blanked[pos:idx])
            raw.append(lines[n - 1])
            text = " ".join(buf).strip()
            if text:
                out.append((start, re.sub(r"\s+", " ", text).upper(), "\n".join(raw)))
            buf, raw = [], []
            pos = idx + 1
    text = " ".join(buf).strip()
    if text:
        out.append((start, re.sub(r"\s+", " ", text).upper(), "\n".join(raw)))
    return out


def static_check(lines: list[str]) -> list[str]:
    """Violations of the static ruleset, with line numbers."""
    violations: list[str] = []
    stmts = statements(lines)
    if not stmts or not stmts[0][1].startswith("REPORT"):
        violations.append("the program must start with a REPORT statement")

    itabs: set[str] = set()
    for _n, text, _raw in stmts:
        for pattern in ITAB_DECL:
            for m in pattern.finditer(text):
                itabs.add(m.group(1))

    for n, text, raw in stmts:
        for pattern, message in FORBIDDEN:
            if re.search(pattern, text):
                violations.append(f"line {n}: {message}")
                break
        for pattern, message in ITAB_TARGET:
            m = pattern.search(text)
            if m and m.group(1) not in itabs and not m.group(1).startswith("("):
                violations.append(f"line {n}: {message} ({m.group(1)})")
        # Function names live inside literals, which `text` has blanked.
        for m in CALL_FUNCTION_RE.finditer(re.sub(r"\s+", " ", raw).upper()):
            name = m.group(1).upper()
            if not config.function_allowed(name):
                violations.append(f"line {n}: function module {name} is not allowed")
        if SELECT_RE.match(text):
            for m in FROM_RE.finditer(text):
                table = m.group(1)
                for pattern in config.READ_TABLE_DENY:
                    if fnmatch.fnmatchcase(table, pattern.upper()):
                        violations.append(f"line {n}: table {table} is not allowed")
                        break
            m = UP_TO_RE.search(text)
            if m:
                if int(m.group(1)) > config.ABAP_MAX_SELECT_ROWS:
                    violations.append(
                        f"line {n}: UP TO {m.group(1)} ROWS exceeds the limit of "
                        f"{config.ABAP_MAX_SELECT_ROWS}")
            elif " WHERE " not in text and not re.search(r"\b(COUNT|MAX|MIN|SUM|AVG)\s*\(", text):
                violations.append(
                    f"line {n}: SELECT without UP TO n ROWS or WHERE is not allowed")
    return violations


async def llm_audit(llm, source: str, model: str | None = None) -> dict:
    """The model's verdict; never raises -- an unavailable auditor is a verdict too."""
    messages = [
        {"role": "system", "content": auditor_prompt()},
        {"role": "user", "content": f"Program:\n```abap\n{source}\n```"},
    ]
    # Reasoning models spend output tokens on hidden thinking before the
    # JSON; a small budget then yields an empty reply. Start with the
    # configured output budget and double it once when nothing usable comes.
    budget = max(int(config.LLM_MAX_OUTPUT_TOKENS), 1024)
    obj = None
    text = ""
    for attempt in range(2):
        try:
            text = await llm.complete(messages, model=model or config.LLM_AUDIT_MODEL,
                                      temperature=0.0, json_mode=True, max_tokens=budget)
        except LLMError as exc:
            log.warning("audit call failed: %s", exc)
            return {"allowed": False, "risk": "high", "violations": [],
                    "summary": f"security review unavailable: {exc}", "unavailable": True}
        obj = parse_json_object(text)
        if obj:
            break
        log.warning("audit returned no JSON (attempt %d, budget %d): %r", attempt + 1, budget,
                    (text or "")[:200])
        budget *= 2
    if not obj:
        return {"allowed": False, "risk": "high", "violations": [],
                "summary": "security review returned no verdict", "unavailable": True}
    risk = str(obj.get("risk", "high")).lower()
    return {
        "allowed": bool(obj.get("allowed", False)),
        "risk": risk if risk in ("low", "medium", "high") else "high",
        "violations": [str(v) for v in obj.get("violations", []) or []],
        "summary": str(obj.get("summary", "")).strip(),
    }
