"""curie --trace: the timeline of one session log, one line per event.

    curie --trace logs/sessions/eval/2026-09-12/0ED4F5DF....jsonl
    curie --trace 0ED4F5DF          # a session id prefix, searched under logs/sessions
    curie --trace --newest          # the newest session of any kind
    curie --trace --newest eval     # the newest evaluation session; "user" for a real one
    curie --trace 0ED4F5DF --full   # answers, prompts and tool results in full

Every line starts with the seconds since the session opened, then the
event kind and what matters for that kind: the model call with its
budget, tools and notes; the answer with its finish reason, output
tokens and duration; tool calls with the table or name; syntax errors
with the word; repairs; the audit verdict and whether it came from the
memo; notices; the close reason. Business data stays in the log: the
default output shows the first line of texts and results, `--full` shows
them whole.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

SHORT = 100


def opened_at(path: Path) -> float:
    """When the session opened: the timestamp of the log's first record,
    written by Curie. File times are not used for ordering: an editor's
    save changes the modification time, a copy or a restore the creation
    time, and Linux has no creation time at all. The file's modification
    time is the fallback for an unreadable first line."""
    try:
        with path.open(encoding="utf-8") as fh:
            first = fh.readline()
        ts = float(json.loads(first).get("ts") or 0)
        if ts > 0:
            return ts
    except (OSError, ValueError, AttributeError):
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


KINDS = {"any": "**/*.jsonl", "eval": "eval/*/*.jsonl", "user": "*/*.jsonl"}


def find_log(spec: str, log_dir: str | Path, last: bool = False, kind: str = "any") -> Path | None:
    """The log file for a path, a session id prefix (case-insensitive,
    searched in every day folder, eval included), or with `last` the
    newest session of `kind`: "any", "eval" (evaluation sessions under
    eval/) or "user" (real sessions in the day folders); "newest" by the
    session's own open time."""
    root = Path(log_dir)
    if last:
        candidates = sorted(root.glob(KINDS.get(kind, KINDS["any"])), key=opened_at)
        return candidates[-1] if candidates else None
    if not spec:
        return None
    path = Path(spec)
    if path.is_file():
        return path
    prefix = spec.upper()
    hits = [p for p in root.glob("**/*.jsonl") if p.stem.upper().startswith(prefix)]
    if not hits:
        return None
    hits.sort(key=opened_at)
    return hits[-1]


def _first_line(text, full: bool) -> str:
    text = " ".join(str(text or "").split()) if not full else str(text or "")
    if full:
        return text
    return text[:SHORT] + ("..." if len(text) > SHORT else "")


def _fmt_result(result, full: bool) -> str:
    if result is None:
        return ""
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(result)
    return _first_line(text, full)


def describe(rec: dict, full: bool = False) -> str | None:
    """What one record says; None for records the timeline leaves out."""
    kind = rec.get("kind", "")
    if kind == "session_open":
        sap = rec.get("sap") or {}
        return f"{sap.get('sysid', '?')}/{sap.get('client', '?')} {sap.get('user', '?')}, release {sap.get('release', '?')}"
    if kind == "user_message":
        flags = []
        if rec.get("followup"):
            flags.append("follow-up")
        if rec.get("light"):
            flags.append("light")
        return f"{_first_line(rec.get('text'), full)}" + (f"  [{', '.join(flags)}]" if flags else "")
    if kind == "llm_user":
        if not rec.get("auto"):
            return None                       # the user_message record already said it
        return "auto: " + _first_line(rec.get("text"), full)
    if kind == "llm_call":
        notes = list(rec.get("notes") or []) + list(rec.get("exemplars") or [])
        shown = ", ".join(notes[:3]) + (f" +{len(notes) - 3}" if len(notes) > 3 else "")
        return (f"budget {rec.get('max_tokens', '?')} tokens, {rec.get('tools', 0)} tools"
                + (", light" if rec.get("light") else "")
                + f", knowledge {rec.get('knowledge_chars', 0)} chars" + (f", notes: {shown}" if shown else ""))
    if kind == "llm_done":
        ms = int(rec.get("ms") or 0)
        return (f"{rec.get('finish') or '?'}, {rec.get('tokens_out', '?')} tokens out, "
                f"{rec.get('tool_calls', 0)} tool call(s), {ms / 1000:.1f} s")
    if kind == "llm_assistant":
        return _first_line(rec.get("text"), full)
    if kind == "llm_truncated":
        return f"asking again with {rec.get('budget')}"
    if kind == "llm_interrupted":
        return "interrupted" + (": " + _first_line(rec.get("text"), full) if rec.get("text") else "")
    if kind == "tool_call":
        args = rec.get("args") or {}
        what = args.get("table") or args.get("name") or args.get("query") or ""
        return f"{rec.get('name', '?')} {what}".strip()
    if kind == "tool":
        ms = int(rec.get("ms") or 0)
        text = f"{'ok' if rec.get('ok') else 'FAILED'}, {ms} ms: {_first_line(rec.get('summary'), False)}"
        if full and rec.get("result") is not None:
            text += "\n" + " " * 24 + _fmt_result(rec.get("result"), True)
        return text
    if kind == "tool_result":
        return None                           # the tool record carries the same and more
    if kind == "code_checked":
        parts = []
        if rec.get("repairs"):
            parts.append("repairs: " + "; ".join(rec["repairs"]))
        if rec.get("static_problems"):
            parts.append("static: " + "; ".join(rec["static_problems"]))
        return f"{rec.get('progname', '')} " + ("; ".join(parts) if parts else "no static problems")
    if kind == "syntax_error":
        return f"{rec.get('word', '')}: {_first_line(rec.get('message'), full)} (line {rec.get('line')}, attempt {rec.get('attempt')})"
    if kind == "audit":
        verdict = "allowed" if rec.get("allowed") else "DENIED"
        memo = ", from the memo" if rec.get("memo") else ""
        unavailable = ", reviewer unavailable" if rec.get("unavailable") else ""
        return f"{verdict}, risk {rec.get('risk', '?')}{memo}{unavailable}: {_first_line(rec.get('summary'), full)}"
    if kind == "code_ready":
        return f"{rec.get('progname', '')} can_run={bool(rec.get('can_run'))}" + \
            (f", replayed {rec.get('origin', {}).get('family') or rec.get('origin', {}).get('catalogue')}"
             if rec.get("origin") and (rec["origin"].get("family") or rec["origin"].get("catalogue")) else "")
    if kind in ("code_confirmed", "run_started"):
        return f"{rec.get('progname', '')} {rec.get('job', '')}".strip()
    if kind == "run_result":
        lines = rec.get("lines") or []
        return f"{'ok' if rec.get('ok') else 'FAILED'}, {len(lines)} line(s)" + \
            (f": {_first_line(rec.get('message'), full)}" if rec.get("message") else "")
    if kind in ("catalogue_replay", "family"):
        return f"{rec.get('note')} with {rec.get('values')}" + (" (routed by the model)" if rec.get("routed") else "")
    if kind == "notice":
        return f"[{rec.get('level', 'info')}] {_first_line(rec.get('text'), full)}"
    if kind == "feedback":
        counted = "" if rec.get("counted", True) else ", not counted"
        return f"{rec.get('target')} {rec.get('id')}: {rec.get('value')} by {rec.get('user', '?')}{counted}"
    if kind == "thread_outcome":
        return f"{'ok' if rec.get('ok') else 'wrong'} ({rec.get('how')}): used {len(rec.get('used') or [])}, unused {len(rec.get('unused') or [])}"
    if kind in ("exemplar_saved", "insight_saved", "hint_from_thread"):
        return f"{rec.get('note')}"
    if kind == "session_closed":
        return f"{rec.get('reason', '')}"
    if kind in ("assistant_start", "assistant_done", "assistant_delta", "state"):
        return None
    return _first_line({k: v for k, v in rec.items() if k not in ("ts", "kind")}, full)


def timeline(path: str | Path, full: bool = False) -> list[str]:
    """The lines of the timeline; the first names the file."""
    path = Path(path)
    out = [f"{path}"]
    t0 = None
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                out.append("        ???             unreadable line")
                continue
            ts = float(rec.get("ts") or 0)
            if t0 is None:
                t0 = ts
                out[0] += "  (" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) + ")"
            text = describe(rec, full)
            if text is None:
                continue
            out.append(f"{ts - t0:7.1f}s  {rec.get('kind', '?'):15} {text}")
    return out
