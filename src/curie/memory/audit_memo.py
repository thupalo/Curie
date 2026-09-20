"""Audit memoisation (P11): a program whose normalised shape equals one the
reviewer already allowed keeps that verdict and skips the second model
round trip (a median 22 seconds per program in the archive).

The key blanks string literals (a different agreement number is the same
program) but keeps table names, function names, numbers and everything
else the auditor judges; a literal that moved into a table name changes
the key. Programs with dynamic names (`FROM (lv_tab)`, `CALL FUNCTION
lv_name`) are never memoised: their meaning is in the literals. The key
carries the version of the rules (the auditor prompt, the static ruleset,
the audit model), so a changed rule invalidates every verdict.

Entries come from the archive at start (programs the reviewer allowed and
the user confirmed or ran) and from the running session; the session log
and the archive keep the verdicts, so nothing else is persisted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time

from curie import config
from curie.sap_tools.abap_source import REPORT_LINE, _split_comment

log = logging.getLogger(__name__)

# The word before a literal that makes the literal a name, not a value.
NAME_KEYWORDS = {"FUNCTION", "CALL", "DESTINATION", "PROGRAM", "TRANSACTION", "DATASET", "ID",
                 "REPORT", "SUBMIT", "METHOD", "PERFORM", "COMMAND", "OBJECT"}
DYNAMIC = re.compile(r"\b(FROM|JOIN|INTO|TABLE|UPDATE|MODIFY|DELETE|INSERT)\s+\(|"
                     r"\bCALL\s+FUNCTION\s+[A-Za-z_]|\bPERFORM\s+\(|\bIN\s+PROGRAM\s+\(", re.IGNORECASE)
LITERAL = re.compile(r"'(?:[^']|'')*'|`(?:[^`]|``)*`")


def normalise(source: str) -> str:
    """The comparable form of a program: no REPORT line, no comments, string
    literals blanked unless they name a function or a program, whitespace
    collapsed, upper case."""
    text = (source or "").replace("\r\n", "\n")
    text = REPORT_LINE.sub("", text, count=1)
    out: list[str] = []
    for line in text.split("\n"):
        if line.startswith("*"):
            continue
        code, _ = _split_comment(line)
        if code.strip():
            out.append(code)
    code = " ".join(out)

    def blank(m: re.Match) -> str:
        before = code[:m.start()].rstrip().split()
        prev = before[-1].upper() if before else ""
        if prev in NAME_KEYWORDS:
            return m.group(0).upper()
        return "'?'"

    code = LITERAL.sub(blank, code)
    return re.sub(r"\s+", " ", code).strip().upper()


def memoisable(source: str) -> bool:
    return not DYNAMIC.search(source or "")


def rules_version() -> str:
    """Changes whenever the rules the verdict rests on change."""
    from curie.llm import audit
    from curie.llm.prompts import auditor_prompt

    material = auditor_prompt() + "\n" + repr(audit.FORBIDDEN) + "\n" + repr(audit.ITAB_TARGET) \
        + "\n" + str(config.LLM_AUDIT_MODEL) + "\n" + ",".join(config.READ_TABLE_DENY)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


def key(source: str, rules: str | None = None) -> str:
    rules = rules or rules_version()
    return hashlib.sha1((rules + "\n" + normalise(source)).encode("utf-8")).hexdigest()[:24]


class AuditMemo:
    def __init__(self, enabled: bool | None = None):
        self.enabled = config.ABAP_AUDIT_MEMO if enabled is None else enabled
        self.entries: dict[str, dict] = {}
        self.rules = rules_version()
        self.hits = 0

    def __len__(self) -> int:
        return len(self.entries)

    def lookup(self, source: str) -> dict | None:
        """The stored verdict for an equal program shape, worded so the card
        says where it came from; None when there is none."""
        if not self.enabled or not memoisable(source):
            return None
        entry = self.entries.get(key(source, self.rules))
        if entry is None:
            return None
        self.hits += 1
        when = time.strftime("%Y-%m-%d", time.localtime(entry.get("ts") or time.time()))
        return {"allowed": True, "risk": entry.get("risk", "low"), "violations": [],
                "summary": f"{entry.get('summary', '')} (same program shape allowed on {when}, "
                           f"run {entry.get('run_id', '?')}; review not repeated)".strip(),
                "memo": True, "memo_run": entry.get("run_id", "")}

    def remember(self, source: str, verdict: dict, run_id: str, ts: float | None = None) -> bool:
        """Keep an allowed verdict of the reviewer itself (never a memo hit,
        never an unavailable reviewer)."""
        if not self.enabled or not verdict or not verdict.get("allowed") or verdict.get("memo") \
                or verdict.get("unavailable") or not memoisable(source):
            return False
        k = key(source, self.rules)
        if k in self.entries:
            return False
        self.entries[k] = {"risk": verdict.get("risk", "low"), "summary": verdict.get("summary", ""),
                           "run_id": run_id, "ts": ts or time.time()}
        return True

    def load(self, archive) -> int:
        """Verdicts from the archive: programs the reviewer allowed and the
        user then confirmed (or that ran). Returns the number added."""
        if not self.enabled or archive is None:
            return 0
        added = 0
        try:
            rows = archive.query(
                "SELECT run_id, ts, audit, source FROM programs "
                "WHERE can_run = 1 AND (confirmed = 1 OR run_ok = 1) AND audit IS NOT NULL "
                "ORDER BY ts")
        except Exception as exc:  # noqa: BLE001 -- an old archive never blocks serving
            log.warning("audit memo: archive not readable: %s", exc)
            return 0
        for row in rows:
            try:
                verdict = row["audit"] if isinstance(row["audit"], dict) else json.loads(row["audit"] or "{}")
            except (TypeError, ValueError):
                continue
            if self.remember(row.get("source") or "", verdict, row.get("run_id") or "", row.get("ts")):
                added += 1
        return added
