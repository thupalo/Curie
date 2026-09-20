import asyncio

from curie import config
from curie.llm.audit import llm_audit, static_check, statements


def src(*lines):
    return ["REPORT zai_test."] + list(lines)


def test_statements_split_outside_literals_and_comments():
    stmts = statements(["REPORT z.", "* comment. with. dots.", "WRITE 'a.b'. \" c.",
                        "DATA: x TYPE i,", "      y TYPE i."])
    assert [s[1] for s in stmts] == ["REPORT Z", "WRITE ' '", "DATA: X TYPE I, Y TYPE I"]
    assert [s[0] for s in stmts] == [1, 3, 4]


def test_clean_program_passes():
    lines = src("DATA: lt_mara LIKE mara OCCURS 0 WITH HEADER LINE.",
                "SELECT * FROM mara INTO TABLE lt_mara UP TO 10 ROWS",
                "  WHERE mtart = 'FERT'.",
                "LOOP AT lt_mara.",
                "  WRITE: / lt_mara-matnr.",
                "ENDLOOP.",
                "DELETE lt_mara WHERE mtart = 'X'.",
                "MODIFY lt_mara FROM lt_mara INDEX 1.")
    assert static_check(lines) == []


def test_database_changes_are_caught():
    assert any("UPDATE" in v for v in static_check(src("UPDATE mara SET mtart = 'X'.")))
    assert any("DELETE" in v for v in static_check(src("DELETE FROM mara WHERE matnr = 'A'.")))
    assert any("DELETE" in v for v in static_check(src("DELETE mara WHERE matnr = 'A'.")))
    assert any("MODIFY" in v for v in static_check(src("MODIFY mara FROM wa.")))
    assert any("INSERT" in v for v in static_check(src("INSERT INTO mara VALUES wa.")))
    assert any("INSERT" in v for v in static_check(src("INSERT mara FROM wa.")))
    assert any("COMMIT" in v for v in static_check(src("COMMIT WORK.")))
    # internal table forms are fine
    lines = src("DATA lt TYPE STANDARD TABLE OF mara.", "DATA wa LIKE mara.",
                "INSERT wa INTO TABLE lt.", "MODIFY lt FROM wa INDEX 1.", "DELETE lt INDEX 1.")
    assert static_check(lines) == []


def test_system_access_and_dialogs_are_caught():
    for stmt, word in [
        ("CALL 'SYSTEM' ID 'COMMAND' FIELD 'dir'.", "kernel"),
        ("SUBMIT zother AND RETURN.", "SUBMIT"),
        ("CALL TRANSACTION 'SE38'.", "CALL TRANSACTION"),
        ("OPEN DATASET lv_f FOR OUTPUT.", "DATASET"),
        ("CALL FUNCTION 'X' DESTINATION 'Y'.", "DESTINATION"),
        ("CALL FUNCTION 'BAPI_TRANSACTION_COMMIT'.", "BAPI_TRANSACTION_COMMIT"),
        ("CALL FUNCTION 'BAPI_MATERIAL_SAVEDATA'.", "not allowed"),
        ("PARAMETERS p_x TYPE c.", "selection"),
        ("MESSAGE e001(00).", "MESSAGE"),
        ("INSERT REPORT 'Z' FROM lt.", "INSERT REPORT"),
        ("EXEC SQL.", "native SQL"),
    ]:
        violations = static_check(src(stmt))
        assert violations, stmt
        assert word.lower() in " ".join(violations).lower(), (stmt, violations)


def test_select_bounds(monkeypatch):
    monkeypatch.setattr(config, "ABAP_MAX_SELECT_ROWS", 100)
    monkeypatch.setattr(config, "READ_TABLE_DENY", ["USR02"])
    assert static_check(src("SELECT * FROM mara INTO TABLE lt.")) != []
    assert static_check(src("SELECT * FROM mara INTO TABLE lt UP TO 500 ROWS.")) != []
    assert static_check(src("SELECT * FROM mara INTO TABLE lt UP TO 50 ROWS.")) == []
    assert static_check(src("SELECT * FROM mara INTO TABLE lt WHERE mtart = 'F'.")) == []
    assert static_check(src("SELECT COUNT(*) FROM mara INTO lv_n.")) == []
    assert static_check(src("SELECT SINGLE * FROM mara INTO wa.")) == []
    assert any("USR02" in v for v in static_check(src("SELECT * FROM usr02 INTO TABLE lt UP TO 1 ROWS.")))


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    async def complete(self, messages, **kwargs):
        assert messages[0]["role"] == "system"
        return self.reply


def test_llm_audit_parses_and_defaults():
    ok = asyncio.run(llm_audit(FakeLLM('{"allowed": true, "risk": "low", "violations": [], "summary": "fine"}'), "REPORT z."))
    assert ok["allowed"] is True and ok["risk"] == "low"
    fenced = asyncio.run(llm_audit(FakeLLM('```json\n{"allowed": false, "risk": "weird", "violations": ["x"]}\n```'), "x"))
    assert fenced["allowed"] is False and fenced["risk"] == "high" and fenced["violations"] == ["x"]
    broken = asyncio.run(llm_audit(FakeLLM("no json here"), "x"))
    assert broken["allowed"] is False and broken.get("unavailable")


def test_function_policy_from_config(monkeypatch):
    monkeypatch.setattr(config, "ABAP_FUNCTION_POLICY", "deny")
    monkeypatch.setattr(config, "ABAP_FUNCTION_DENY", ["TH_*", "BAPI_*_SAVE*"])
    monkeypatch.setattr(config, "ABAP_FUNCTION_ALLOW", ["TH_DISPLAY_USER_LIST"])
    assert static_check(src("CALL FUNCTION 'TH_USER_INFO'.")) != []
    assert static_check(src("CALL FUNCTION 'TH_DISPLAY_USER_LIST'.")) == []   # allow wins
    assert static_check(src("CALL FUNCTION 'READ_TEXT'.")) == []
    monkeypatch.setattr(config, "ABAP_FUNCTION_POLICY", "allow")
    monkeypatch.setattr(config, "ABAP_FUNCTION_ALLOW", ["READ_TEXT", "CONVERSION_EXIT_*"])
    assert static_check(src("CALL FUNCTION 'READ_TEXT'.")) == []
    assert static_check(src("CALL FUNCTION 'CONVERSION_EXIT_ALPHA_INPUT'.")) == []
    assert any("POPUP_TO_CONFIRM" in v for v in static_check(src("CALL FUNCTION 'POPUP_TO_CONFIRM'.")))
