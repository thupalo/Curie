import pytest

from curie.sap_tools.abap_source import (encoding_problems, extract_abap_blocks, fold_line,
                                   new_progname, prepare_source, sanitize_for_codepage,
                                   strip_comments)


def test_extract_blocks_case_insensitive():
    text = "Here:\n```abap\nREPORT z.\nWRITE 'x'.\n```\nand\n```ABAP\nWRITE 'y'.\n```"
    blocks = extract_abap_blocks(text)
    assert blocks == ["REPORT z.\nWRITE 'x'.", "WRITE 'y'."]
    assert extract_abap_blocks("```python\nprint(1)\n```") == []


def test_prepare_source_replaces_report_line_and_folds():
    code = "REPORT zai_temp NO STANDARD PAGE HEADING.\n" \
           "DATA: lv_a(10) TYPE c, lv_b(10) TYPE c, lv_c(10) TYPE c, lv_d(10) TYPE c, lv_e(10) TYPE c.\n" \
           "WRITE: / 'hello'.\n\n"
    lines, problems = prepare_source(code, "ZAI_ABCD1234", 72)
    assert problems == []
    assert lines[0] == "REPORT zai_abcd1234."
    assert all(len(line) <= 72 for line in lines)
    assert lines[-1] == "WRITE: / 'hello'."
    assert " ".join(l.strip() for l in lines[1:-1]).startswith("DATA: lv_a(10) TYPE c,")


def test_fold_line_keeps_literals_and_comments_intact():
    long_literal = "WRITE: / '" + "x" * 60 + "', lv_field, lv_other_field."
    out = fold_line(long_literal, 72)
    assert all(len(l) <= 72 for l in out)
    assert "'" + "x" * 60 + "'" in out[0]

    comment = "  lv_a = lv_b. \" this comment is rather long and needs folding into more than one line"
    out = fold_line(comment, 72)
    assert out[0] == "  lv_a = lv_b."
    assert all(l.startswith('" ') for l in out[1:])

    star = "* " + "word " * 30
    out = fold_line(star, 72)
    assert all(l.startswith("* ") and len(l) <= 72 for l in out)


def test_fold_line_impossible_reports_problem():
    with pytest.raises(ValueError):
        fold_line("WRITE '" + "y" * 80 + "'.", 72)
    lines, problems = prepare_source("WRITE '" + "y" * 80 + "'.", "ZAI_X", 72)
    assert problems and "cannot fold" in problems[0]


def test_encoding_problems_for_4_5b_codepage():
    assert encoding_problems(["WRITE 'zażółć'."], "iso-8859-2") == []
    bad = encoding_problems(["WRITE '日本'."], "iso-8859-2")
    assert bad and "line 1" in bad[0]


def test_sanitize_transliterates_and_replaces():
    lines = ["* Import parameter SERVER – character field (“length” 1…)",
             "WRITE 'zażółć'.", "WRITE '日本'."]
    out, notes = sanitize_for_codepage(lines, "iso-8859-2")
    assert out[0] == '* Import parameter SERVER - character field ("length" 1...)'
    assert out[1] == "WRITE 'zażółć'."          # Polish is fine in 8859-2
    assert out[2] == "WRITE '??'."
    assert len(notes) == 2 and notes[0].startswith("line 1") and notes[1].startswith("line 3")
    assert sanitize_for_codepage(["WRITE 'plain'."], "iso-8859-2") == (["WRITE 'plain'."], [])


def test_strip_comments_and_progname():
    assert strip_comments(["* full", "a = b. \" tail", "c = 'x\"y'."]) == ["", "a = b.", "c = 'x\"y'."]
    name = new_progname("ZAI_")
    assert name.startswith("ZAI_") and len(name) == 12


# -- deterministic repairs (P11) ---------------------------------------------------- #

from curie.sap_tools.abap_source import dialect_problems, repair_source  # noqa: E402


def test_repair_adds_the_period_before_else_and_endif():
    lines = ["REPORT zai_x.", "IF sy-subrc <> 0.", "  WRITE: / 'Brak umow.'", "ELSE.", "  WRITE: / 'ok',",
             "ENDIF."]
    out, notes = repair_source(lines)
    assert out[2] == "  WRITE: / 'Brak umow.'." and out[4] == "  WRITE: / 'ok'."
    assert notes == ["line 3: period added before ELSE", "line 5: period added before ENDIF"]
    # A statement that visibly continues is left to SAP.
    out, notes = repair_source(["REPORT zai_x.", "IF a = 1 AND", "ENDIF."])
    assert out[1] == "IF a = 1 AND" and notes == []


def test_repair_moves_indented_comment_markers_but_not_multiplication():
    lines = ["REPORT zai_x.", "LOOP AT lt INTO wa.", "  * limit the output", "  lv = lv", "  * 2.", "ENDLOOP."]
    out, notes = repair_source(lines)
    assert out[2] == "* limit the output" and out[4] == "  * 2."
    assert notes == ["line 3: comment marker moved to column 1"]


def test_repair_moves_up_to_before_where():
    lines = ["REPORT zai_x.", "SELECT * FROM ZV_UM_KLI_PRZED", "  INTO TABLE lt_lines", "  WHERE NRREJ LIKE 'DX%'",
             "  UP TO 50 ROWS.", "LOOP AT lt_lines INTO wa.", "ENDLOOP."]
    out, notes = repair_source(lines)
    assert out[1:5] == ["SELECT * FROM ZV_UM_KLI_PRZED", "  INTO TABLE lt_lines", "  UP TO 50 ROWS",
                        "  WHERE NRREJ LIKE 'DX%'."]
    assert notes == ["line 4: UP TO 50 ROWS moved before WHERE"]
    one, notes = repair_source(["REPORT z.", "SELECT nrum FROM zumowyl INTO TABLE lt WHERE rokum = '2005' UP TO 10 ROWS."])
    assert one[1] == "SELECT nrum FROM zumowyl INTO TABLE lt UP TO 10 ROWS WHERE rokum = '2005'."
    # Already in place, SELECT SINGLE, or no WHERE: untouched.
    for src in (["REPORT z.", "SELECT nrum FROM zumowyl UP TO 10 ROWS INTO TABLE lt WHERE rokum = '2005'."],
                ["REPORT z.", "SELECT SINGLE nrum FROM zumowyl INTO lv WHERE rokum = '2005'."],
                ["REPORT z.", "SELECT nrum FROM zumowyl INTO TABLE lt UP TO 10 ROWS."]):
        assert repair_source(src) == (src, [])


def test_dialect_problems_name_7_40_constructs_on_45b_only():
    lines = ["REPORT z.", "DATA(lv) = 1.", "SELECT * FROM t INTO TABLE @lt.", "WRITE |x{ y }|.",
             "lv = VALUE #( ).", "* DATA( in a comment", "WRITE 'a|b'.", "lv = lv * 2."]
    problems = dialect_problems(lines, "45B")
    assert [p.split(":")[0] for p in problems] == ["line 2", "line 3", "line 4", "line 5"]
    assert "inline DATA(...)" in problems[0] and "4.5B" in problems[0]
    assert dialect_problems(lines, "ECC6") == []


def test_repair_spells_out_desc_and_asc_in_order_by_and_sort():
    lines = ["REPORT z.", "SELECT nrum wartosc FROM zumowyl INTO TABLE lt", "  WHERE rokum = '2005'",
             "  ORDER BY wartosc DESC nrum ASC.", "SORT lt BY wartosc DESC.", "WRITE: / 'DESC', lv_desc.",
             "SELECT * FROM t INTO TABLE lt2 UP TO 5 ROWS ORDER BY a DESC. \" DESC kept in the comment"]
    out, notes = repair_source(lines)
    assert out[3] == "  ORDER BY wartosc DESCENDING nrum ASCENDING."
    assert out[4] == "SORT lt BY wartosc DESCENDING."
    assert out[5] == "WRITE: / 'DESC', lv_desc."                       # a literal and a longer name stay
    assert out[6] == "SELECT * FROM t INTO TABLE lt2 UP TO 5 ROWS ORDER BY a DESCENDING. \" DESC kept in the comment"
    assert notes == ["line 4: DESC/ASC written as DESCENDING/ASCENDING",
                     "line 5: DESC/ASC written as DESCENDING/ASCENDING",
                     "line 7: DESC/ASC written as DESCENDING/ASCENDING"]
    assert repair_source(["REPORT z.", "DATA desc TYPE c.", "desc = 'x'."])[1] == []   # outside SELECT/SORT
