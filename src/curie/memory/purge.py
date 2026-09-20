"""Purge what Curie learned: the last N minutes, or everything.

A test session leaves notes, reports and archived episodes behind that
nobody wants to keep; a switch to another SAP system calls for a clean
slate. `curie --purge-knowledge --last 15m` moves to
knowledge/.trash/<timestamp>/ (never a delete: move things back to undo,
or empty .trash when sure):

* the notes of the window, of every type: tables/, concepts/,
  families/, exemplars/, hints/, insights/ (`--types` narrows);
* the reports written in the window: EVAL*.md and REVIEW.md;
* and removes the archived episodes of the window (the chat history in
  archive/curie.db) unless --no-archive.

`--all` purges every note and report, moves the archive file and the
compiled bundle (.build) to the trash, and asks twice. knowledge/CARD.md
and README.md are never touched.
"""
from __future__ import annotations

import logging
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from curie.memory.store import KnowledgeStore, Note

log = logging.getLogger(__name__)

LEARNED_TYPES = ("exemplar", "hint", "insight")
ALL_TYPES = ("table", "concept", "family", "exemplar", "hint", "insight")
REPORT_PATTERNS = ("EVAL*.md", "REVIEW.md")
UNITS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600, "d": 86400, "day": 86400,
         "w": 604800}
INTERVAL = re.compile(r"^\s*(?:last\s+)?(\d+(?:[.,]\d+)?)\s*([a-z]+)?\s*$", re.IGNORECASE)


def parse_interval(text: str) -> float:
    """'15m', 'last 15min', '2h', '1 day' -> seconds; a bare number is minutes."""
    m = INTERVAL.match(text or "")
    if not m:
        raise ValueError(f"cannot read the interval {text!r}; use 15m, 2h, 1d")
    value = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "m").lower().rstrip("s") or "s"
    if unit not in UNITS:
        unit = unit + "s" if unit + "s" in UNITS else unit
    if unit not in UNITS:
        raise ValueError(f"unknown unit in {text!r}; use s, m, h, d")
    return value * UNITS[unit]


def note_time(path: Path) -> float:
    """When the note came into being: the earliest timestamp the OS keeps."""
    st = path.stat()
    times = [st.st_mtime]
    birth = getattr(st, "st_birthtime", None)
    if birth:
        times.append(birth)
    elif sys.platform == "win32":
        times.append(st.st_ctime)
    return min(times)


def select_reports(store: KnowledgeStore, since_seconds: float | None = None,
                   everything: bool = False) -> list[Path]:
    """The report files (EVAL*.md, REVIEW.md) written in the window, or all."""
    if since_seconds is None and not everything:
        return []
    limit = time.time() - (since_seconds or 0)
    out: list[Path] = []
    for pattern in REPORT_PATTERNS:
        for path in sorted(store.root.glob(pattern)):
            if path.is_file() and (everything or path.stat().st_mtime >= limit):
                out.append(path)
    return out


def select(store: KnowledgeStore, since_seconds: float | None = None, everything: bool = False,
           types: tuple[str, ...] = ALL_TYPES) -> list[Note]:
    """Notes to purge: of the given types, and within the window unless everything."""
    if since_seconds is None and not everything:
        return []
    limit = time.time() - (since_seconds or 0)
    out = []
    for note in store.notes(include_retired=True):
        if note.type not in types:
            continue
        if everything or note_time(note.path) >= limit:
            out.append(note)
    out.sort(key=lambda n: n.id)
    return out


def trash_dir(store: KnowledgeStore) -> Path:
    return store.root / ".trash" / datetime.now().strftime("%Y%m%d-%H%M%S")


def purge(store: KnowledgeStore, notes: list[Note], reports: list[Path] = (),
          trash: Path | None = None) -> Path | None:
    """Move the notes and report files to knowledge/.trash/<timestamp>/
    keeping their folders."""
    if not notes and not reports:
        return None
    trash = trash or trash_dir(store)
    moved: set[Path] = set()
    for note in notes:
        target = trash / note.path.relative_to(store.root)
        if not note.path.exists() or note.path in moved:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(note.path), str(target))
        moved.add(note.path)
        log.info("purged %s -> %s", note.id, target.relative_to(store.root).as_posix())
    for path in reports:
        # A report the store also read as a note is already in the trash.
        if not path.exists() or path in moved:
            continue
        target = trash / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        moved.add(path)
        log.info("purged %s -> %s", path.name, target.relative_to(store.root).as_posix())
    store.refresh(force=True)
    return trash


def purge_archive(archive_path: str | Path | None, since_seconds: float | None, everything: bool,
                  trash: Path) -> str:
    """Remove the archived episodes of the window, or move the whole
    archive file to the trash; returns a sentence for the user."""
    if not archive_path:
        return ""
    path = Path(archive_path)
    if not path.exists():
        return ""
    if everything:
        target = trash / "archive"
        target.mkdir(parents=True, exist_ok=True)
        moved = []
        for extra in ("", "-wal", "-shm", "-journal"):
            side = path.with_name(path.name + extra)
            if side.exists():
                shutil.move(str(side), str(target / side.name))
                moved.append(side.name)
        log.info("purged archive %s -> %s", path, target)
        return f"archive {', '.join(moved)} moved to {target}"
    from curie.archive import Archive

    archive = Archive(path)
    try:
        removed = archive.purge(since=time.time() - (since_seconds or 0))
    finally:
        archive.close()
    return f"{removed} archived episode(s) removed"


def count_archive(archive_path: str | Path | None, since_seconds: float | None, everything: bool) -> int:
    """How many episodes the purge would remove (for the confirmation)."""
    if not archive_path or not Path(archive_path).exists():
        return 0
    from curie.archive import Archive

    archive = Archive(archive_path)
    try:
        if everything:
            return int(archive.stats().get("episodes", 0))
        return len(archive.episodes(limit=100000, since=time.time() - (since_seconds or 0)))
    except Exception as exc:  # noqa: BLE001
        log.warning("archive not counted: %s", exc)
        return 0
    finally:
        archive.close()


def purge_build(build_dir: str | Path | None, trash: Path) -> str:
    """Move the compiled bundle to the trash (it is rebuilt from the notes)."""
    if not build_dir:
        return ""
    path = Path(build_dir)
    if not path.is_dir():
        return ""
    target = trash / ".build"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))
    return f"bundle {path.name} moved to {target}"


def describe(notes: list[Note]) -> str:
    lines = []
    for n in notes:
        when = datetime.fromtimestamp(note_time(n.path)).strftime("%Y-%m-%d %H:%M")
        head = (n.question or n.body.strip().splitlines()[0] if n.body.strip() else n.name)[:80]
        lines.append(f"  {when}  {n.id:<40} [{n.type}, {n.status}] {head}")
    return "\n".join(lines)


def confirm(prompt: str, expected: str = "y") -> bool:
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.lower() == expected.lower() if expected != "y" else answer.lower() in ("y", "yes")


def run(store: KnowledgeStore, last: str | None = None, everything: bool = False,
        types: str = "", yes: bool = False, out=print, archive_path: str | Path | None = None,
        with_reports: bool = True, build_dir: str | Path | None = None) -> int:
    """The CLI flow. Returns the number of items purged (notes, report files
    and archived episodes), -1 when refused."""
    chosen_types = tuple(t.strip().lower() for t in types.split(",") if t.strip()) or ALL_TYPES
    since = parse_interval(last) if last else None
    if since is None and not everything:
        out("Nothing selected: give --last <interval> (e.g. 15m) or --all.")
        return 0
    notes = select(store, since, everything, chosen_types)
    reports = select_reports(store, since, everything) if with_reports else []
    episodes = count_archive(archive_path, since, everything)
    if not notes and not reports and not episodes:
        out("Nothing matches.")
        return 0
    scope = "everything" if everything else f"the last {last}"
    out(f"To purge ({scope}): {len(notes)} note(s) of types {', '.join(chosen_types)}, "
        f"{len(reports)} report file(s), {episodes} archived episode(s)"
        + (", the archive file and the bundle" if everything and archive_path else "") + ".")
    if notes:
        out(describe(notes))
    if reports:
        out("  reports: " + ", ".join(p.name for p in reports))
    total = len(notes) + len(reports) + episodes
    if everything:
        # Two confirmations, the second one typed in full: this empties what
        # Curie has learned so far.
        if not confirm("Move every note, report, the archive and the bundle to .trash? [y/N] "):
            out("Nothing purged.")
            return -1
        if not confirm(f"Type PURGE {total} to confirm: ", expected=f"PURGE {total}"):
            out("Nothing purged.")
            return -1
    elif not yes and not confirm(f"Purge {total} item(s)? [y/N] "):
        out("Nothing purged.")
        return -1
    trash = trash_dir(store)
    purge(store, notes, reports, trash)
    lines = [f"Moved {len(notes)} note(s) and {len(reports)} report file(s) to {trash}."]
    if archive_path and (episodes or everything):
        lines.append(purge_archive(archive_path, since, everything, trash))
    if everything:
        lines.append(purge_build(build_dir, trash))
    out(" ".join(l for l in lines if l) + " Move things back to undo; delete .trash when sure.")
    return total

