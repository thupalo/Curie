"""Purging learned notes: interval parsing, selection by time, the trash folder."""
import os
import time

import pytest

from curie.memory import purge
from tests.test_knowledge import make_store

PROGRAM = "REPORT z.\nSELECT SINGLE * FROM zumowyl INTO wa WHERE rokum = '2004'.\nWRITE wa."


def test_parse_interval():
    assert purge.parse_interval("15m") == 900
    assert purge.parse_interval("last 15min") == 900
    assert purge.parse_interval("2h") == 7200 and purge.parse_interval("1 day") == 86400
    assert purge.parse_interval("30") == 1800 and purge.parse_interval("90s") == 90
    with pytest.raises(ValueError):
        purge.parse_interval("soon")


def test_select_by_window_and_purge_to_trash(tmp_path):
    store = make_store(tmp_path)
    fresh = store.add_exemplar("Ile umów zawarto w 2004 roku?", PROGRAM, "T", "00000001")
    hint = store.add_hint("Rok umowy to ROKUM.", "T", ["ZUMOWYL"])
    old = store.add_insight("Keep every SELECT bounded with UP TO n ROWS.", source="curie:rules")
    ancient = time.time() - 3 * 3600
    os.utime(old.path, (ancient, ancient))
    # every type is a candidate; the window decides (the curated notes of
    # the fixture are older than the window)
    curated = [n for n in store.notes(include_retired=True) if n.type in ("table", "concept")]
    for n in curated:
        os.utime(n.path, (ancient, ancient))
    chosen = purge.select(store, since_seconds=900)
    assert [n.id for n in chosen] == sorted([fresh.id, hint.id])
    everything = [n.id for n in purge.select(store, everything=True)]
    assert fresh.id in everything and old.id in everything and "tables/ZUMOWYL" in everything
    assert purge.select(store) == []
    assert [n.id for n in purge.select(store, everything=True, types=("table",))] == ["tables/ZUMOWYL"]
    assert [n.id for n in purge.select(store, since_seconds=900, types=purge.LEARNED_TYPES)] == sorted([fresh.id, hint.id])

    lines = []
    purged = purge.run(store, last="15m", yes=True, out=lines.append)
    assert purged == 2 and lines[0].startswith("To purge (the last 15m): 2 note(s)")
    assert store.get(fresh.id) is None and store.get(hint.id) is None and store.get(old.id) is not None
    trash = [p for p in (tmp_path / ".trash").rglob("*.md")]
    assert sorted(p.parent.name for p in trash) == ["exemplars", "hints"]
    # the trash is not part of the store: two curated, one retired, the old insight
    assert len(store.notes(include_retired=True)) == 4
    assert purge.run(store, last="15m", yes=True, out=lines.append) == 0 and lines[-1] == "Nothing matches."
    assert purge.run(store, out=lines.append) == 0


def test_refused_without_confirmation(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.add_hint("Rok umowy to ROKUM.", "T", ["ZUMOWYL"])
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert purge.run(store, last="1h", out=lambda s: None) == -1
    assert len([n for n in store.notes() if n.type == "hint"]) == 1


def test_reports_and_archive_go_with_the_window(tmp_path):
    from curie.archive import Archive
    from tests.test_archive import new_format_session, write_log

    store = make_store(tmp_path)
    for n in store.notes(include_retired=True):
        ancient = time.time() - 3 * 3600
        os.utime(n.path, (ancient, ancient))
    (tmp_path / "EVAL.md").write_text("# Evaluation", encoding="utf-8")
    (tmp_path / "EVAL_old.md").write_text("# Evaluation, older", encoding="utf-8")
    (tmp_path / "REVIEW.md").write_text("# Review", encoding="utf-8")
    (tmp_path / "README.md").write_text("never purged", encoding="utf-8")
    (tmp_path / "CARD.md").write_text("never purged", encoding="utf-8")
    old_report = time.time() - 3 * 3600
    os.utime(tmp_path / "EVAL_old.md", (old_report, old_report))
    logs = tmp_path / "logs"
    write_log(logs / "2026-09-11" / "AAAA1111.jsonl", new_format_session(time.time() - 120))
    write_log(logs / "2026-09-09" / "BBBB2222.jsonl", new_format_session(time.time() - 3 * 86400))
    db = tmp_path / "archive" / "curie.db"
    archive = Archive(db)
    archive.import_dir(logs)
    assert archive.stats()["episodes"] == 4
    archive.close()

    assert [p.name for p in purge.select_reports(store, since_seconds=900)] == ["EVAL.md", "REVIEW.md"]
    assert purge.count_archive(db, 900, False) == 2
    lines = []
    purged = purge.run(store, last="15m", yes=True, out=lines.append, archive_path=db)
    assert purged == 4 and "0 note(s)" in lines[0] and "2 report file(s), 2 archived episode(s)" in lines[0]
    assert not (tmp_path / "EVAL.md").exists() and (tmp_path / "EVAL_old.md").exists()
    assert (tmp_path / "README.md").exists() and (tmp_path / "CARD.md").exists()
    assert sorted(p.name for p in (tmp_path / ".trash").rglob("*.md")) == ["EVAL.md", "REVIEW.md"]
    archive = Archive(db)
    assert archive.stats()["episodes"] == 2                  # the older session stays
    archive.close()
    # --no-reports / no archive: only notes are considered
    assert purge.run(store, last="15m", yes=True, out=lines.append, with_reports=False) == 0


def test_everything_moves_the_archive_file_and_the_bundle(tmp_path, monkeypatch):
    from curie.archive import Archive
    from tests.test_archive import new_format_session, write_log

    store = make_store(tmp_path)
    (tmp_path / "REVIEW.md").write_text("# Review", encoding="utf-8")
    logs = tmp_path / "logs"
    write_log(logs / "2026-09-11" / "AAAA1111.jsonl", new_format_session())
    db = tmp_path / "archive" / "curie.db"
    a = Archive(db); a.import_dir(logs); a.close()
    build = tmp_path / ".build"
    build.mkdir()
    (build / "manifest.json").write_text("{}", encoding="utf-8")
    answers = iter(["y", None])
    total = len(store.notes(include_retired=True)) + 1 + 2      # notes, one report, two episodes

    def fake_input(prompt):
        a = next(answers)
        return a if a is not None else f"PURGE {total}"
    monkeypatch.setattr("builtins.input", fake_input)
    lines = []
    purged = purge.run(store, everything=True, out=lines.append, archive_path=db, build_dir=build)
    assert purged == total
    assert store.notes(include_retired=True) == [] and not (tmp_path / "REVIEW.md").exists()
    assert not db.exists() and not build.exists()
    trash = next((tmp_path / ".trash").iterdir())
    assert (trash / "archive" / "curie.db").exists() and (trash / ".build" / "manifest.json").exists()
    assert "the archive file and the bundle" in lines[0]


def test_root_files_are_reports_not_notes_and_are_moved_once(tmp_path):
    """EVAL_DS.md at the root used to be read as a concept note and then
    moved twice by --all: once as a note, once as a report (FileNotFound)."""
    store = make_store(tmp_path)
    (tmp_path / "EVAL_DS.md").write_text("---\ntype: concept\nname: x\n---\n# Evaluation with model DS",
                                         encoding="utf-8")
    (tmp_path / "REVIEW.md").write_text("# Review", encoding="utf-8")
    (tmp_path / "CARD.md").write_text("card", encoding="utf-8")
    store.refresh(force=True)
    assert not any(n.id in ("EVAL_DS", "REVIEW", "CARD") for n in store.notes(include_retired=True))
    reports = purge.select_reports(store, everything=True)
    assert [p.name for p in reports] == ["EVAL_DS.md", "REVIEW.md"]
    trash = purge.purge(store, purge.select(store, everything=True), reports)
    assert (trash / "EVAL_DS.md").exists() and (trash / "REVIEW.md").exists() and (tmp_path / "CARD.md").exists()
    # a second purge of the same paths (already gone) does not fail
    assert purge.purge(store, [], reports, trash) == trash
