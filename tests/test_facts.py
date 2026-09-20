"""Facts mined from programs, verified, rendered, and drafted table notes."""
from curie.memory import dictionary as dict_mod
from curie.memory import facts as facts_mod
from curie.memory.store import KnowledgeStore
from tests.test_dictionary import build_index
from tests.test_shape import JOIN_VIA_ITAB, SUM_VIA_VARIABLES

STATUS_HISTORY = """REPORT zai_h.
DATA lv_id LIKE zmstan-id_objekt.
DATA: BEGIN OF lt_s OCCURS 0, stan_po LIKE zmstan-stan_po, data LIKE zmstan-data, END OF lt_s.
SELECT SINGLE id_zumowyl FROM zumowyl INTO lv_id WHERE rokum = '2005' AND nrum = '0000100'.
SELECT stan_po data FROM zmstan INTO TABLE lt_s UP TO 500 ROWS
  WHERE id_objekt = lv_id AND grupa_kod = 'UMLEA'.
"""


def test_mine_program_yields_joins_constants_and_value_forms():
    facts = facts_mod.mine_program(SUM_VIA_VARIABLES, "Podaj sumę należności dla umowy 100/2005", evidence="e1")
    triples = {(f.subject, f.predicate, f.object) for f in facts}
    assert ("ZRECZNE.ANULOWANA", "filter_pattern", "= space") in triples
    assert ("ZRECZNE.STORNO", "filter_pattern", "= space") in triples
    assert ("ZRECZNE.NRUM", "value_form",
            "the part 'nrum' of the document key zero padded to 7 characters (the user writes 100/2005)") in triples
    assert not any(f.object == "= '2005'" for f in facts)            # a parameter, not a constant
    facts = facts_mod.mine_program(JOIN_VIA_ITAB, "umowy zawarte w lutym 2005", evidence="e2")
    assert ("ZKNA1.KUNNR", "joins", "ZUMOWYL.KLIENT") in {(f.subject, f.predicate, f.object) for f in facts}
    facts = facts_mod.mine_program(STATUS_HISTORY, "Wskaż okres kiedy umowa 100/2005 była aktywna", evidence="e3")
    triples = {(f.subject, f.predicate, f.object) for f in facts}
    assert ("ZMSTAN.GRUPA_KOD", "filter_pattern", "= 'UMLEA'") in triples
    assert ("ZMSTAN.ID_OBJEKT", "joins", "ZUMOWYL.ID_ZUMOWYL") in triples     # via SELECT ... INTO lv_id
    assert not any(f.predicate == "joins" and f.subject == f.object for f in facts)
    # a year or an id on a key field without a question value is not a convention
    facts = facts_mod.mine_program(STATUS_HISTORY, "Tak, napisz program dla podanej umowy", evidence="e4")
    assert not any(f.predicate == "filter_pattern" and f.object in ("= '2005'", "= '0000100'") for f in facts)


def test_merge_verify_and_promote():
    a = facts_mod.mine_program(SUM_VIA_VARIABLES, "umowa 100/2005", evidence="e1", user="A")
    b = facts_mod.mine_program(SUM_VIA_VARIABLES.replace("0000100", "0000200"), "umowa 200/2005",
                               evidence="e2", user="B")
    merged = facts_mod.merge(a + b)
    storno = next(f for f in merged.values() if f.subject == "ZRECZNE.STORNO")
    assert storno.support == 2 and storno.users == {"A", "B"}
    data = build_index()
    data["tables"]["ZRECZNE"] = {"text": "Faktury PZST", "fields": [{"n": "STORNO", "t": "", "k": 0},
                                                                     {"n": "ANULOWANA", "t": "", "k": 0},
                                                                     {"n": "NRUM", "k": 1}, {"n": "ROKUM", "k": 1}]}
    index = dict_mod.Index(data)
    counts = facts_mod.verify(merged, index)
    assert counts["verified"] == len(merged) and storno.status == "verified"
    assert facts_mod.promote(merged) >= 1 and storno.status == "confirmed"
    # a field the dictionary does not know is contradicted
    ghost = facts_mod.merge([facts_mod.Fact("ZRECZNE.GHOST", "filter_pattern", "= 'X'", ["e9"])])
    facts_mod.verify(ghost, index)
    assert list(ghost.values())[0].status == "contradicted"


def test_render_into_notes_and_synthesise(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge", refresh_every=0)
    (store.root / "tables" / "ZUMOWYL.md").write_text(
        "---\ntype: table\nname: ZUMOWYL\n---\nAgreements.\n", encoding="utf-8")
    store.refresh(force=True)
    facts = facts_mod.merge(facts_mod.mine_program(JOIN_VIA_ITAB, "umowy w lutym 2005", evidence="e1")
                            + facts_mod.mine_program(STATUS_HISTORY, "umowa 100/2005", evidence="e2"))
    index = dict_mod.Index(build_index())
    facts_mod.verify(facts, index)
    touched = facts_mod.render_into_notes(store, facts)
    assert touched == ["tables/ZUMOWYL"]
    note = store.get("tables/ZUMOWYL")
    assert note.body == "Agreements." and any(f["predicate"] == "value_form" for f in note.facts)
    rendered = store.render([note], [])
    assert "ZUMOWYL.NRUM takes the part 'nrum' of the document key zero padded" in rendered
    # a used table without a note is drafted from the index
    data = build_index()
    data["field_usage"] = {"ZUBEZP": {"NRPOLISY": 3}}
    index = dict_mod.Index(data)
    usage = {"ZUBEZP": 5, "ZV_UM_KLI_PRZED": 1}       # confirmed programs, not dictionary reads
    drafted = facts_mod.synthesise_notes(store, index, usage, facts,
                                         glossary={"polis": {"tables": ["ZUBEZP"], "forms": ["polisa", "polisy"]},
                                                   "aktyw": {"tables": ["ZUMOWYL", "ZUBEZP"], "forms": ["aktywna"]}},
                                         co_read={"tables": {"ZUBEZP": {"ZUMOWYL": 2}}})
    assert [n.id for n in drafted] == ["tables/ZUBEZP"]
    note = store.get("tables/ZUBEZP")
    assert note.status == "proposed" and note.aliases == ["polisa", "polisy"]   # not "aktywna": ZUMOWYL's term
    assert "Key: ROKUM, NRUM." in note.body and "NRPOLISY (Numer polisy)" in note.body
    assert "Read together with: ZUMOWYL (2)" in note.body
    assert facts_mod.synthesise_notes(store, index, usage, facts) == []   # not twice
    assert facts_mod.program_usage(store) == {}                          # no exemplars in this store
