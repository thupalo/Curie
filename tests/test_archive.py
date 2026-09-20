"""The archive: session logs become episodes (old and new log formats)."""
import json
import time

import pytest

from curie.archive import Archive, build_session
from curie.archive.episodes import detect_language


def write_log(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def new_format_session(t0=1789000000.0):
    """A session as Curie logs it since 11 September: thread ids, llm_call,
    llm_done, tool records with results."""
    return [
        {"ts": t0, "kind": "session_open", "sap": {"sysid": "TC0", "client": "005", "user": "THUPALO",
                                                   "language": "L", "release": "45B"}, "prompt": ""},
        {"ts": t0 + 1, "kind": "user_message", "id": "u1", "text": "Ile umów zawarto w 2004 roku?",
         "thread": "u1", "followup": False},
        {"ts": t0 + 1, "kind": "llm_user", "auto": False, "text": "Ile umów zawarto w 2004 roku?", "thread": "u1"},
        {"ts": t0 + 1.1, "kind": "assistant_start", "id": "a1"},
        {"ts": t0 + 1.1, "kind": "llm_call", "id": "a1", "thread": "u1", "model": "qwen", "temperature": 0.2,
         "max_tokens": 16000, "tool_mode": "auto", "tools": 5, "notes": ["tables/ZUMOWYL"],
         "exemplars": ["exemplars/X"], "knowledge_chars": 900, "messages": 2, "history_chars": 40},
        {"ts": t0 + 5, "kind": "llm_done", "id": "a1", "finish": "tool_calls", "tool_calls": 1, "chars": 0,
         "ms": 3900, "tokens_in": 1200, "tokens_out": 40},
        {"ts": t0 + 5, "kind": "assistant_done", "id": "a1", "text": "", "interrupted": False},
        {"ts": t0 + 5, "kind": "tool_call", "id": "c1", "name": "sap_get_structure", "args": {"name": "ZUMOWYL"}},
        {"ts": t0 + 5.1, "kind": "tool_result", "id": "c1", "name": "sap_get_structure", "ok": True,
         "summary": "3 field(s)", "ms": 30},
        {"ts": t0 + 5.1, "kind": "tool", "id": "c1", "turn": "a1", "thread": "u1", "name": "sap_get_structure",
         "args": {"name": "ZUMOWYL"}, "ok": True, "summary": "3 field(s)", "ms": 30,
         "result": {"fields": [{"name": "ROKUM"}, {"name": "NRUM"}, {"name": "STAN"}]}},
        {"ts": t0 + 6, "kind": "assistant_start", "id": "a2"},
        {"ts": t0 + 6, "kind": "llm_call", "id": "a2", "thread": "u1", "model": "qwen", "notes": [], "exemplars": []},
        {"ts": t0 + 12, "kind": "llm_done", "id": "a2", "finish": "stop", "tool_calls": 0, "chars": 300,
         "ms": 6000, "tokens_in": 1500, "tokens_out": 200},
        {"ts": t0 + 12, "kind": "assistant_done", "id": "a2", "text": "Program ...", "interrupted": False},
        {"ts": t0 + 12, "kind": "llm_assistant", "text": "Program ..."},
        {"ts": t0 + 12.1, "kind": "code_checked", "run_id": "R1", "progname": "ZAI_1", "static_problems": []},
        {"ts": t0 + 14, "kind": "audit", "run_id": "R1", "allowed": True, "risk": "low", "violations": [],
         "summary": "reads only"},
        {"ts": t0 + 14, "kind": "code_ready", "run_id": "R1", "progname": "ZAI_1",
         "source": "REPORT zai_1.\nSELECT COUNT(*) FROM zumowyl WHERE rokum = '2004'.", "syntax": {"ok": True},
         "audit": {"allowed": True, "risk": "low"}, "can_run": True, "origin": None},
        {"ts": t0 + 20, "kind": "code_confirmed", "run_id": "R1", "progname": "ZAI_1"},
        {"ts": t0 + 20, "kind": "run_started", "run_id": "R1", "job": "ZAI_1/1", "mode": "job"},
        {"ts": t0 + 21, "kind": "run_result", "run_id": "R1", "ok": True, "rc": 0, "message": "", "lines": ["12"]},
        {"ts": t0 + 22, "kind": "exemplar_saved", "note": "exemplars/20260911-R1", "run_id": "R1"},
        {"ts": t0 + 40, "kind": "feedback", "target": "answer", "id": "a2", "value": "up"},
        {"ts": t0 + 60, "kind": "user_message", "id": "u2", "text": "Pomiń kolumnę TOW", "thread": "u1",
         "followup": True},
        {"ts": t0 + 61, "kind": "assistant_start", "id": "a3"},
        {"ts": t0 + 65, "kind": "assistant_done", "id": "a3", "text": "Ok", "interrupted": False},
        {"ts": t0 + 120, "kind": "user_message", "id": "u3", "text": "Ile mandantów jest w T000?",
         "thread": "u3", "followup": False},
        {"ts": t0 + 121, "kind": "assistant_start", "id": "a4"},
        {"ts": t0 + 125, "kind": "assistant_done", "id": "a4", "text": "5", "interrupted": False},
        {"ts": t0 + 300, "kind": "session_closed", "reason": "idle"},
    ]


def old_format_session(t0=1788600000.0):
    """The format before 11 September: no thread ids, tool_call/tool_result
    pairs plus a `tool` record without id, no llm_call."""
    return [
        {"ts": t0, "kind": "session_open", "sap": {"sysid": "TC0", "client": "005", "user": "THUPALO",
                                                   "language": "L", "release": "45B"}, "prompt": ""},
        {"ts": t0 + 1, "kind": "user_message", "id": "u1", "text": "Wskaż okres kiedy umowa 100/2005 była aktywna"},
        {"ts": t0 + 1.1, "kind": "assistant_start", "id": "a1"},
        {"ts": t0 + 3, "kind": "assistant_done", "id": "a1", "text": "", "interrupted": False},
        {"ts": t0 + 3, "kind": "tool_call", "id": "c1", "name": "sap_read_table", "args": {"table": "ZMSTAN"}},
        {"ts": t0 + 3.2, "kind": "tool_result", "id": "c1", "name": "sap_read_table", "ok": False,
         "summary": "unknown field", "ms": 20},
        {"ts": t0 + 3.2, "kind": "tool", "name": "sap_read_table", "args": {"table": "ZMSTAN"}, "ok": False,
         "summary": "unknown field", "ms": 20},
        {"ts": t0 + 4, "kind": "assistant_start", "id": "a2"},
        {"ts": t0 + 9, "kind": "assistant_done", "id": "a2", "text": "Program", "interrupted": False},
        {"ts": t0 + 9.1, "kind": "code_checked", "run_id": "R9", "progname": "ZAI_9", "static_problems": []},
        {"ts": t0 + 9.5, "kind": "syntax_error", "run_id": "R9", "message": "period missing", "line": 3,
         "word": "ELSE", "attempt": 1, "max_attempts": 3},
        {"ts": t0 + 9.5, "kind": "code_ready", "run_id": "R9", "progname": "ZAI_9", "source": "REPORT zai_9.",
         "syntax": {"ok": False, "message": "period missing"}, "audit": None, "can_run": False, "origin": None},
        {"ts": t0 + 10, "kind": "assistant_start", "id": "a3"},
        {"ts": t0 + 15, "kind": "assistant_done", "id": "a3", "text": "Fixed", "interrupted": False},
        {"ts": t0 + 15.1, "kind": "code_checked", "run_id": "R10", "progname": "ZAI_10", "static_problems": []},
        {"ts": t0 + 17, "kind": "audit", "run_id": "R10", "allowed": False, "risk": "high",
         "violations": ["unbounded SELECT"], "summary": "denied"},
        {"ts": t0 + 17, "kind": "code_ready", "run_id": "R10", "progname": "ZAI_10", "source": "REPORT zai_10.",
         "syntax": {"ok": True}, "audit": {"allowed": False, "risk": "high"}, "can_run": False, "origin": None},
        {"ts": t0 + 30, "kind": "user_message", "id": "u2", "text": "Wygeneruj jeszcze raz poprawiając problem"},
        {"ts": t0 + 31, "kind": "assistant_start", "id": "a4"},
        {"ts": t0 + 35, "kind": "assistant_done", "id": "a4", "text": "", "interrupted": True},
        {"ts": t0 + 35, "kind": "llm_interrupted", "text": ""},
        {"ts": t0 + 50, "kind": "session_closed", "reason": "browser_gone"},
    ]


def test_new_format_episodes(tmp_path):
    path = tmp_path / "logs" / "2026-09-11" / "AAAA1111.jsonl"
    write_log(path, new_format_session())
    session, episodes = build_session(path)
    assert session.user == "THUPALO" and session.reason == "idle"
    assert [e.question for e in episodes] == ["Ile umów zawarto w 2004 roku?", "Ile mandantów jest w T000?"]
    first = episodes[0]
    assert first.refinements == ["Pomiń kolumnę TOW"]
    assert first.outcome == "confirmed_ran_ok"
    assert first.language == "PL"
    assert len(first.calls) == 3 and first.calls[0]["notes"] == ["tables/ZUMOWYL"]
    assert first.tokens_in == 2700 and first.tokens_out == 240
    assert len(first.tool_calls) == 1 and first.tool_calls[0]["result"]["fields"][2]["name"] == "STAN"
    assert first.programs[0]["run"]["ok"] and first.programs[0]["exemplar_id"] == "exemplars/20260911-R1"
    assert first.feedback == [{"target": "answer", "id": "a2", "value": "up", "ts": 1789000040.0}]
    assert first.next_action == "question" and 50 < first.next_gap < 60
    assert episodes[1].outcome == "answered" and episodes[1].next_action == "close"


def test_old_format_episodes(tmp_path):
    path = tmp_path / "logs" / "2026-09-09" / "BBBB2222.jsonl"
    write_log(path, old_format_session())
    session, episodes = build_session(path)
    assert len(episodes) == 1                      # "Wygeneruj jeszcze raz" is a follow-up
    ep = episodes[0]
    assert ep.refinements == ["Wygeneruj jeszcze raz poprawiając problem"]
    assert len(ep.tool_calls) == 1 and ep.tool_calls[0]["ok"] is False   # the pair, not the duplicate
    assert [p["run_id"] for p in ep.programs] == ["R9", "R10"]
    assert ep.syntax_rounds == 1
    assert ep.outcome == "denied"
    assert ep.next_action == "close"


def test_session_across_midnight_is_one_session(tmp_path):
    root = tmp_path / "logs"
    records = new_format_session()
    write_log(root / "2026-09-10" / "CCCC3333.jsonl", records[:20])
    write_log(root / "2026-09-11" / "CCCC3333.jsonl", records[20:])
    session, episodes = build_session(root / "2026-09-11" / "CCCC3333.jsonl")
    assert session.opened > 0 and session.reason == "idle"
    assert len(episodes) == 2 and episodes[0].outcome == "confirmed_ran_ok"


def test_archive_import_is_idempotent_and_purgeable(tmp_path):
    root = tmp_path / "logs"
    write_log(root / "2026-09-11" / "AAAA1111.jsonl", new_format_session())
    write_log(root / "2026-09-09" / "BBBB2222.jsonl", old_format_session())
    archive = Archive(tmp_path / "archive" / "curie.db")
    stats = archive.import_dir(root)
    assert stats == {"logs": 2, "imported": 2, "unchanged": 0, "episodes": 3}
    again = archive.import_dir(root)
    assert again["imported"] == 0 and again["unchanged"] == 2
    st = archive.stats()
    assert st["sessions"] == 2 and st["episodes"] == 3 and st["programs"] == 3
    assert st["outcomes"] == {"answered": 1, "confirmed_ran_ok": 1, "denied": 1}
    assert st["tokens_in"] == 2700
    rows = archive.query("SELECT name, tables FROM tool_calls ORDER BY ts")
    assert json.loads(rows[0]["tables"]) == ["ZMSTAN"]
    programs = archive.query("SELECT run_id, tables, run_ok FROM programs WHERE run_ok = 1")
    assert programs[0]["run_id"] == "R1" and json.loads(programs[0]["tables"]) == ["ZUMOWYL"]
    # Re-import after the log changed replaces the rows, never duplicates them.
    time.sleep(0.01)
    write_log(root / "2026-09-11" / "AAAA1111.jsonl", new_format_session())
    (root / "2026-09-11" / "AAAA1111.jsonl").touch()
    archive.import_dir(root, force=True)
    assert archive.stats()["episodes"] == 3
    # Purge by time: the new-format session is the recent one.
    removed = archive.purge(since=1789000000.0)
    assert removed == 2 and archive.stats()["episodes"] == 1
    assert archive.import_dir(root)["imported"] == 0     # purged sessions are not re-imported
    archive.close()


def test_language_detection():
    assert detect_language("Ile umów zawarto w 2004 roku?") == "PL"
    assert detect_language("Show me the contents of table T000") == "EN"
    assert detect_language("Who are you?") == "EN"
    assert detect_language("T000") == ""


@pytest.mark.parametrize("text", ["Tak. Napisz program, który wyznaczy dane środka trwałego",
                                  "Skoryguj powyższe wskazówki", "Jaki to był pojazd?",
                                  "Zobacz opis pozycji dla tej faktury", "Użyj proszę funkcji X"])
def test_more_followups_detected(text):
    from curie.memory.threads import is_followup
    assert is_followup(text)
