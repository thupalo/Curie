"""Audit memoisation: equal program shapes keep the reviewer's verdict."""
from curie.memory import audit_memo as am

A = "REPORT zai_1.\n* count\nSELECT COUNT(*) FROM zumowyl INTO lv WHERE rokum = '2005'. \" c\nWRITE lv."
B = "REPORT zai_2.\nSELECT COUNT(*) FROM zumowyl INTO lv\n  WHERE rokum = '2006'.\nWRITE lv."
C = "REPORT zai_3.\nSELECT COUNT(*) FROM zumowyl INTO lv WHERE rokum = '2006' AND stan = 'A'.\nWRITE lv."


def test_normalise_blanks_values_but_keeps_names_and_numbers():
    assert am.normalise(A) == am.normalise(B) == "SELECT COUNT(*) FROM ZUMOWYL INTO LV WHERE ROKUM = '?'. WRITE LV."
    assert am.normalise(A) != am.normalise(C)
    assert am.normalise("CALL FUNCTION 'Z_X' EXPORTING a = 'b'.") == "CALL FUNCTION 'Z_X' EXPORTING A = '?'."
    assert am.normalise("SELECT * FROM t UP TO 50 ROWS.") != am.normalise("SELECT * FROM t UP TO 500 ROWS.")
    assert am.memoisable(A) and not am.memoisable("SELECT * FROM (lv_tab) INTO TABLE lt.")
    assert not am.memoisable("CALL FUNCTION lv_name.")


def test_memo_remembers_allowed_verdicts_and_answers_equal_shapes():
    memo = am.AuditMemo(enabled=True)
    assert memo.lookup(B) is None
    assert not memo.remember(A, {"allowed": False, "risk": "high"}, "R0")
    assert not memo.remember(A, {"allowed": True, "risk": "low", "memo": True}, "R0")
    assert not memo.remember(A, {"allowed": True, "unavailable": True}, "R0")
    assert memo.remember(A, {"allowed": True, "risk": "low", "summary": "reads only"}, "R1", ts=1789000000.0)
    assert not memo.remember(B, {"allowed": True, "risk": "low"}, "R2")       # same shape, already there
    hit = memo.lookup(B)
    assert hit["allowed"] and hit["memo"] and hit["memo_run"] == "R1" and hit["risk"] == "low"
    assert hit["summary"].startswith("reads only (same program shape allowed on 2026-09-") and "R1" in hit["summary"]
    assert memo.lookup(C) is None and memo.hits == 1
    assert am.AuditMemo(enabled=False).lookup(A) is None


def test_memo_loads_confirmed_programs_from_the_archive():
    class FakeArchive:
        def query(self, sql, args=()):
            assert sql.startswith("SELECT run_id")
            return [{"run_id": "R1", "ts": 1.0, "audit": '{"allowed": true, "risk": "medium", "summary": "s"}',
                     "source": A},
                    {"run_id": "R2", "ts": 2.0, "audit": '{"allowed": true}', "source": B},        # same shape
                    {"run_id": "R3", "ts": 3.0, "audit": "not json", "source": C}]

    memo = am.AuditMemo(enabled=True)
    assert memo.load(FakeArchive()) == 1 and len(memo) == 1
    assert memo.lookup(B)["memo_run"] == "R1" and memo.lookup(B)["risk"] == "medium"


def test_key_changes_with_the_rules(monkeypatch):
    from curie import config
    k1 = am.key(A)
    monkeypatch.setattr(config, "LLM_AUDIT_MODEL", "another-model")
    assert am.key(A) != k1
