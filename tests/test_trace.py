"""curie --trace: the timeline of a session log."""
from curie.tools import trace as tr
from tests.test_archive import new_format_session, write_log


def test_find_log_by_path_prefix_and_last(tmp_path):
    root = tmp_path / "logs"
    write_log(root / "2026-09-11" / "AAAA1111BBBB.jsonl", new_format_session())
    write_log(root / "eval" / "2026-09-11" / "EEEE0000.jsonl", new_format_session(1789000100.0))
    write_log(root / "eval" / "2026-09-12" / "EEEE9999.jsonl", new_format_session(1789100000.0))
    assert tr.find_log(str(root / "2026-09-11" / "AAAA1111BBBB.jsonl"), root).name == "AAAA1111BBBB.jsonl"
    assert tr.find_log("aaaa1111", root).name == "AAAA1111BBBB.jsonl"
    assert tr.find_log("EEEE", root) is not None                      # a prefix in the eval folder
    assert tr.find_log("", root, last=True).name == "EEEE9999.jsonl"           # any kind, newest open time
    assert tr.find_log("", root, last=True, kind="eval").name == "EEEE9999.jsonl"
    assert tr.find_log("", root, last=True, kind="user").name == "AAAA1111BBBB.jsonl"
    assert tr.find_log("ZZZZ", root) is None and tr.find_log("", root) is None


def test_timeline_reads_the_events_people_look_for(tmp_path):
    path = tmp_path / "S.jsonl"
    records = new_format_session()
    records.insert(17, {"ts": 1789000012.05, "kind": "code_checked", "run_id": "R1", "progname": "ZAI_1",
                        "static_problems": [], "repairs": ["line 4: period added before ENDIF"]})
    records.insert(18, {"ts": 1789000012.06, "kind": "syntax_error", "run_id": "R1", "word": "DESC",
                        "message": 'Unknown column name "DESC".', "line": 9, "attempt": 1})
    write_log(path, records)
    lines = tr.timeline(path)
    text = "\n".join(lines)
    assert lines[0].startswith(str(path)) and "(2026-" in lines[0]
    assert "session_open" in lines[1] and "TC0/005 THUPALO" in lines[1]
    assert "user_message    Ile umów zawarto w 2004 roku?" in text
    assert "llm_call        budget 16000 tokens, 5 tools" in text and "notes: tables/ZUMOWYL, exemplars/X" in text
    assert "llm_done        tool_calls, 40 tokens out, 1 tool call(s), 3.9 s" in text
    assert "tool_call       sap_get_structure ZUMOWYL" in text
    assert "tool            ok, 30 ms: 3 field(s)" in text
    assert "repairs: line 4: period added before ENDIF" in text
    assert 'syntax_error    DESC: Unknown column name "DESC". (line 9, attempt 1)' in text
    assert "audit           allowed, risk low: reads only" in text
    assert "run_result      ok, 1 line(s)" in text and "feedback        answer a2: up by ?" in text
    assert "session_closed  idle" in text
    assert "assistant_delta" not in text and "tool_result" not in text
    # --full prints the tool result and whole texts.
    full = "\n".join(tr.timeline(path, full=True))
    assert '"fields"' in full and "ROKUM" in full


def test_newest_is_decided_by_the_sessions_own_timestamp(tmp_path):
    import os
    root = tmp_path / "logs"
    older = root / "eval" / "2026-09-11" / "OLDER000.jsonl"
    newer = root / "eval" / "2026-09-12" / "NEWER000.jsonl"
    write_log(older, new_format_session(1789000000.0))
    write_log(newer, new_format_session(1789100000.0))
    # An editor saved the older file after the newer one was written.
    os.utime(older, (1800000000, 1800000000))
    assert older.stat().st_mtime > newer.stat().st_mtime
    assert tr.find_log("", root, last=True) == newer
    assert tr.opened_at(newer) == 1789100000.0
