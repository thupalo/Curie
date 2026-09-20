"""Retention for session logs and the archive."""
import time

from curie.archive import Archive
from curie.memory import housekeeping as hk
from curie.memory.evaluate import asks_for_program
from tests.test_archive import new_format_session, write_log


def test_old_log_folders_are_imported_then_removed(tmp_path):
    root = tmp_path / "logs"
    old = new_format_session(t0=1700000000.0)                     # November 2023
    write_log(root / "2023-11-14" / "OLD00001.jsonl", old)
    write_log(root / "2026-09-11" / "NEW00001.jsonl", new_format_session())
    write_log(root / "eval" / "2023-11-14" / "EVAL0001.jsonl", old)
    archive = Archive(tmp_path / "archive" / "curie.db")
    now = 1789000000.0 + 5 * 86400                                 # five days after the new session
    dry = hk.run(root, archive, log_days=180, archive_days=180, dry_run=True, now=now)
    assert sorted(dry["log_folders_removed"]) == ["2023-11-14", "eval\\2023-11-14"] or \
        sorted(dry["log_folders_removed"]) == ["2023-11-14", "eval/2023-11-14"]
    assert (root / "2023-11-14").exists() and archive.stats()["sessions"] == 0
    report = hk.run(root, archive, log_days=180, archive_days=0, dry_run=False, now=now)
    assert report["logs_imported"] == 1 and len(report["log_folders_removed"]) == 2
    assert not (root / "2023-11-14").exists() and not (root / "eval" / "2023-11-14").exists()
    assert (root / "2026-09-11").exists()
    assert archive.stats()["sessions"] == 1                         # the old session survived in the archive
    # Archive retention removes the old episodes but keeps facts and eval runs.
    archive.import_dir(root)
    assert archive.stats()["episodes"] == 4
    archive.save_eval({"started": now, "seconds": 1, "model": "m", "summary": {"cases": 0}, "cases": []})
    report = hk.run(root, archive, log_days=0, archive_days=180, dry_run=False, now=now)
    assert report["episodes_removed"] == 2 and archive.stats()["episodes"] == 2
    assert archive.last_eval() is not None
    archive.close()


def test_zero_retention_keeps_everything(tmp_path):
    root = tmp_path / "logs"
    write_log(root / "2023-11-14" / "OLD00001.jsonl", new_format_session(t0=1700000000.0))
    report = hk.run(root, None, log_days=0, archive_days=0, now=time.time())
    assert report["log_folders_removed"] == [] and (root / "2023-11-14").exists()


def test_program_requests_are_recognised():
    assert asks_for_program("Napisz program ABAP, który wypisze 5 pierwszych mandantów")
    assert asks_for_program("Tak. Napisz program, który wyznaczy dane środka trwałego")
    assert asks_for_program("Write a program that lists the VINs")
    assert asks_for_program("Utwórz raport sprzedaży")
    assert not asks_for_program("Ile umów zawarto w 2004?")
    assert not asks_for_program("Pokaż zawartość tabeli T000")
