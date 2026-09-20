"""Program shapes, literal binding and typed question values."""
from curie.memory import shape as sh

COUNT_BY_YEAR = """REPORT zai_1.
* comment with a period. inside
DATA lv_count TYPE i.
SELECT COUNT(*) FROM zumowyl
  INTO lv_count
  WHERE ROKUM = '2005'.
WRITE: / 'Liczba umow w roku 2005:', lv_count.
"""

SUM_VIA_VARIABLES = """REPORT zai_2.
DATA lv_nrum     LIKE zreczne-nrum.
DATA lv_rokum    LIKE zreczne-rokum.
DATA lv_saldo    LIKE zreczne-saldo_nal.
lv_rokum = '2005'.
lv_nrum  = '0000100'.
SELECT SUM( saldo_nal ) FROM zreczne INTO lv_saldo
       WHERE rokum = lv_rokum
         AND nrum  = lv_nrum
         AND anulowana = space
         AND storno    = space.
WRITE: / 'Umowa:', lv_nrum, '/', lv_rokum, lv_saldo.
"""

JOIN_VIA_ITAB = """REPORT zai_3.
DATA: BEGIN OF lt_u OCCURS 0,
        nrum   LIKE zumowyl-nrum,
        klient LIKE zumowyl-klient,
      END OF lt_u.
DATA: BEGIN OF lt_k OCCURS 0,
        kunnr LIKE zkna1-kunnr,
        pstlz LIKE zkna1-pstlz,
      END OF lt_k.
SELECT nrum klient FROM zumowyl INTO TABLE lt_u UP TO 100000 ROWS
  WHERE dataum BETWEEN '20050201' AND '20050228'.
LOOP AT lt_u.
  SELECT SINGLE kunnr pstlz FROM zkna1 INTO lt_k WHERE kunnr = lt_u-klient.
ENDLOOP.
DELETE ADJACENT DUPLICATES FROM lt_k COMPARING kunnr.
"""


def test_statements_ignore_comments_and_periods_in_strings():
    st = sh.statements(COUNT_BY_YEAR)
    assert st[0].startswith("REPORT zai_1")
    assert any(s.startswith("SELECT COUNT(*) FROM zumowyl") for s in st)
    assert any("'Liczba umow w roku 2005:'" in s for s in st)
    assert not any("comment" in s for s in st)


def test_shape_of_a_count_by_year():
    shape = sh.analyse(COUNT_BY_YEAR)
    assert shape.tables == ["ZUMOWYL"]
    assert shape.aggregates == ["COUNT"] and shape.filters == ["ZUMOWYL.ROKUM:="]
    assert [(l.value, l.key, l.op) for l in shape.literals] == [("2005", "ZUMOWYL.ROKUM", "=")]
    same = sh.analyse(COUNT_BY_YEAR.replace("2005", "2004"))
    assert same.signature() == shape.signature()
    other = sh.analyse(COUNT_BY_YEAR.replace("ROKUM = '2005'", "DATAUM >= '20050101'"))
    assert other.signature() != shape.signature()


def test_literals_through_variables_and_constants():
    shape = sh.analyse(SUM_VIA_VARIABLES)
    lits = {l.key: l for l in shape.literals if l.value}
    assert lits["ZRECZNE.ROKUM"].value == "2005" and lits["ZRECZNE.ROKUM"].via == "LV_ROKUM"
    assert lits["ZRECZNE.NRUM"].value == "0000100"
    spaces = [l for l in shape.literals if not l.value]
    assert {l.field for l in spaces} == {"ANULOWANA", "STORNO"}
    assert shape.aggregates == ["SUM"]


def test_joins_and_between():
    shape = sh.analyse(JOIN_VIA_ITAB)
    assert shape.tables == ["ZKNA1", "ZUMOWYL"]
    assert shape.joins == ["ZKNA1.KUNNR=ZUMOWYL.KLIENT"]
    between = [l for l in shape.literals if l.op == "BETWEEN"]
    assert [(l.value, l.role) for l in between] == [("20050201", "start"), ("20050228", "end")]
    assert any(l.op == "UP TO" and l.value == "100000" for l in shape.literals)


def test_question_values_are_typed():
    vals = sh.question_values("Podaj sumę faktur dla umowy 100/2005 zawartej w lutym 2005, max 50 PLN 'FORD'")
    types = [(v.type, v.text) for v in vals]
    assert ("key", "100/2005") in types
    assert ("month", "lutym 2005") in types
    assert ("number", "50") in types and ("quoted", "FORD") in types
    key = next(v for v in vals if v.type == "key")
    assert key.renderings == {"text": "100/2005", "nrum": "0000100", "rokum": "2005"}
    month = next(v for v in vals if v.type == "month")
    assert month.renderings["start"] == "20050201" and month.renderings["end"] == "20050228"
    assert [v.type for v in sh.question_values("Ile umów zawarto w 2004r?")] == ["year"]
    period = sh.question_values("umowy z okresu styczeń-luty 2004r")[0]
    assert period.type == "period" and period.renderings["end"] == "20040229"
    assert sh.masked_question("Ile umów zawarto w 2004r?") == "Ile umów zawarto w {year}?"
    code = [v for v in sh.question_values("saldo klienta 125812 w jednostce CARC") if v.type == "code"]
    assert code[0].text == "CARC"


def test_bind_names_the_parameters_and_templatizes():
    shape = sh.analyse(SUM_VIA_VARIABLES)
    params = sh.bind("Podaj sumę należności dla umowy 100/2005", shape)
    assert {(p.name, p.type, p.role, p.key) for p in params} == {
        ("rokum", "key", "rokum", "ZRECZNE.ROKUM"), ("nrum", "key", "nrum", "ZRECZNE.NRUM")}
    template = sh.templatize(SUM_VIA_VARIABLES, params)
    assert "lv_rokum = {{rokum}}." in template and "lv_nrum  = {{nrum}}." in template
    filled = sh.fill(template, {p.name: p.as_dict() for p in params},
                     sh.question_values("Podaj sumę należności dla umowy 7/2004"))
    assert filled is not None
    program, values = filled
    assert "lv_rokum = '2004'." in program and "lv_nrum  = '0000007'." in program
    assert values == {"rokum": "2004", "nrum": "0000007"}
    assert sh.fill(template, {p.name: p.as_dict() for p in params},
                   sh.question_values("Podaj sumę należności w 2004")) is None


def test_bind_year_and_month_forms():
    shape = sh.analyse(COUNT_BY_YEAR)
    params = sh.bind("Ile umów zawarto w 2005r?", shape)
    assert [(p.name, p.type, p.role) for p in params] == [("rokum", "year", "year")]
    # the year inside the WRITE text becomes a bare hole, so a replay for
    # 2003 does not print 2005
    template = sh.templatize(COUNT_BY_YEAR, params)
    assert "WHERE ROKUM = {{rokum}}." in template and "w roku {{rokum:bare}}:'" in template
    program, values = sh.fill(template, {p.name: p.as_dict() for p in params},
                              sh.question_values("Ile umów zawarto w 2003r?"))
    assert "WHERE ROKUM = '2003'." in program and "'Liczba umow w roku 2003:'" in program and "2005" not in program
    shape = sh.analyse(JOIN_VIA_ITAB)
    params = sh.bind("Pokaż umowy zawarte w lutym 2005 według województw", shape)
    assert {(p.name, p.role) for p in params} == {("dataum_start", "start"), ("dataum_end", "end")}
    template = sh.templatize(JOIN_VIA_ITAB, params)
    filled = sh.fill(template, {p.name: p.as_dict() for p in params},
                     sh.question_values("Pokaż umowy zawarte w listopadzie 2003"))
    assert filled and "BETWEEN '20031101' AND '20031130'" in filled[0]


def test_templatize_replaces_the_written_form_of_a_key_inside_texts():
    program = ("REPORT z.\nDATA lv_rokum LIKE zreczne-rokum.\nDATA lv_nrum LIKE zreczne-nrum.\n"
               "lv_rokum = '2005'.\nlv_nrum = '0000339'.\n"
               "SELECT * FROM zreczne INTO TABLE lt WHERE rokum = lv_rokum AND nrum = lv_nrum.\n"
               "WRITE: / 'Faktury zakupu przedmiotu - umowa 339/2005'.\n"
               "WRITE: / 'Brak pozycji dla umowy 339/2005.'.")
    shape = sh.analyse(program)
    params = sh.bind("Pokaż dane faktury zakupu przedmiotu dla umowy 339/2005", shape)
    template = sh.templatize(program, params)
    assert "umowa 339/" not in template and "{{rokum:text}}" in template or "{{nrum:text}}" in template
    program2, values = sh.fill(template, {p.name: p.as_dict() for p in params},
                               sh.question_values("Podaj dane faktury zakupu przedmiotu dla umowy 28/2005"))
    assert "umowa 28/2005'" in program2 and "339" not in program2
    assert values == {"rokum": "2005", "nrum": "0000028"}
    # A template built by an older rule leaks the canonical value: detected.
    old = template.replace("{{rokum:text}}", "339/{{rokum:bare}}").replace("{{nrum:text}}", "339/{{rokum:bare}}")
    leaked, _ = sh.fill(old, {p.name: p.as_dict() for p in params},
                        sh.question_values("umowa 28/2005"))
    assert sh.leaked_values({p.name: p.as_dict() for p in params}, leaked,
                            sh.question_values("umowa 28/2005")) == ["339/2005"]
    assert sh.leaked_values({p.name: p.as_dict() for p in params}, program2,
                            sh.question_values("umowa 28/2005")) == []
