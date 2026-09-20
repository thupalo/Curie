"""Housekeeping (`curie --housekeeping`): retention for the session logs
and the archive.

1. Session log day folders older than LOG_RETENTION_DAYS are imported
   into the archive (so nothing is lost that the archive did not have)
   and then removed. Evaluation logs (logs/sessions/eval/<day>) are
   removed without import.
2. Archive episodes older than ARCHIVE_RETENTION_DAYS are removed with
   their calls, tool calls, programs and events; facts and evaluation
   runs stay. The database file is compacted afterwards.

A retention of 0 keeps everything. `dry_run=True` reports what would go.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DAY_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def day_folders(root: Path) -> list[tuple[Path, float]]:
    """(folder, timestamp of the end of that day) for every day folder
    under root and root/eval."""
    out = []
    for base in (root, root / "eval"):
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir()):
            if p.is_dir() and DAY_FOLDER.match(p.name):
                try:
                    day = datetime.strptime(p.name, "%Y-%m-%d")
                except ValueError:
                    continue
                out.append((p, day.timestamp() + 86400))
    return out


def run(log_dir: str | Path, archive, log_days: int, archive_days: int, dry_run: bool = False,
        now: float | None = None) -> dict:
    now = now or time.time()
    report = {"log_folders_removed": [], "logs_imported": 0, "episodes_removed": 0,
              "dry_run": dry_run, "log_days": log_days, "archive_days": archive_days}
    root = Path(log_dir)
    if log_days > 0:
        cutoff = now - log_days * 86400
        for folder, end in day_folders(root):
            if end >= cutoff:
                continue
            is_eval = folder.parent.name == "eval"
            if not is_eval and archive is not None and not dry_run:
                for path in sorted(folder.glob("*.jsonl")):
                    try:
                        if archive.import_log(path) >= 0:
                            report["logs_imported"] += 1
                    except Exception as exc:  # noqa: BLE001
                        log.warning("housekeeping: %s not imported (%s); folder kept", path.name, exc)
                        break
                else:
                    shutil.rmtree(folder, ignore_errors=True)
                    report["log_folders_removed"].append(str(folder.relative_to(root)))
                continue
            if not dry_run:
                shutil.rmtree(folder, ignore_errors=True)
            report["log_folders_removed"].append(str(folder.relative_to(root)))
    if archive_days > 0 and archive is not None:
        cutoff = now - archive_days * 86400
        old = archive.query("SELECT COUNT(*) AS n FROM episodes WHERE started < ?", (cutoff,))
        count = int(old[0]["n"]) if old else 0
        if dry_run:
            report["episodes_removed"] = count
        elif count:
            report["episodes_removed"] = archive.purge(before=cutoff)
            try:
                archive.vacuum()
            except Exception as exc:  # noqa: BLE001
                log.debug("vacuum: %s", exc)
    log.info("housekeeping%s: %d log folder(s) removed (%d log(s) imported first), %d episode(s) removed; "
             "retention logs %dd, archive %dd", " (dry run)" if dry_run else "",
             len(report["log_folders_removed"]), report["logs_imported"], report["episodes_removed"],
             log_days, archive_days)
    return report
