"""Report families: built from confirmed exemplars, matched by typed values."""
from curie.memory import families
from curie.memory.store import KnowledgeStore

COUNT = """REPORT zai_x.
DATA lv_count TYPE i.
SELECT COUNT(*) FROM zumowyl INTO lv_count WHERE rokum = '{year}'.
WRITE: / 'Liczba umow w roku {year}:', lv_count.
"""

TOP_CUSTOMER = """REPORT zai_y.
DATA: BEGIN OF lt_u OCCURS 0, klient LIKE zumowyl-klient, END OF lt_u.
DATA lv_n TYPE i.
SELECT klient FROM zumowyl INTO TABLE lt_u UP TO 100000 ROWS WHERE rokum = '2004'.
SORT lt_u BY klient.
LOOP AT lt_u.
  lv_n = lv_n + 1.
ENDLOOP.
WRITE: / lv_n.
"""


def add_exemplar(store, question, program, run_id, catalogue=False, successes=0):
    note = store.add_exemplar(question, program, "THUPALO", run_id)
    note.successes = successes
    if catalogue:
        store.mark_catalogue(note.id, on=True)
    else:
        from curie.memory.store import write_note
        write_note(note)
    store.refresh(force=True)
    return note


def make(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge", refresh_every=0)
    add_exemplar(store, "Ile umów zawarto w 2005r?", COUNT.replace("{year}", "2005"), "AAAA0001", successes=3)
    add_exemplar(store, "Ile umów leasingowych zostało zawartych w 2004r?", COUNT.replace("{year}", "2004"),
                 "AAAA0002")
    add_exemplar(store, "Z jakim klientem zawarto najwięcej umów w 2004r", TOP_CUSTOMER, "AAAA0003")
    return store


def test_build_groups_variants_only(tmp_path):
    store = make(tmp_path)
    drafts = families.build(store)
    assert [d.id for d in drafts] == ["zumowyl-count-rokum"]
    d = drafts[0]
    assert d.canonical.id.endswith("AAAA0001") and len(d.members) == 2
    assert {p.name: (p.type, p.role) for p in d.params} == {"rokum": ("year", "year")}
    assert "WHERE rokum = {{rokum}}" in d.template
    assert d.intents == ["Ile umów zawarto w {year}?", "Ile umów leasingowych zostało zawartych w {year}?"]


def test_written_family_is_proposed_until_catalogued(tmp_path):
    store = make(tmp_path)
    notes = families.write(store, families.build(store))
    note = store.get("families/zumowyl-count-rokum")
    assert note is not None and note.type == "family" and note.status == "proposed"
    assert note.params["rokum"]["field"] == "ZUMOWYL.ROKUM" and note.program.count("{{rokum}}") == 1
    assert note.aliases[0] == "Ile umów zawarto w {year}?"
    # Not replayable while proposed; visible as an exemplar-like note.
    assert families.match(store, "Ile umów zawarto w 2003r?") is None
    assert families.match(store, "Ile umów zawarto w 2003r?", replayable_only=False) is not None
    notes_, exemplars = store.retrieve("Ile umów zawarto w 2003 roku?")
    assert any(n.type == "family" for n in exemplars)
    # Mark the canonical exemplar correct: the rebuilt family is confirmed.
    store.mark_catalogue(notes[0].family, on=True)
    families.write(store, families.build(store))
    note = store.get("families/zumowyl-count-rokum")
    assert note.status == "confirmed" and note.catalogue


def test_match_fills_the_template_for_a_new_value(tmp_path):
    store = make(tmp_path)
    store.mark_catalogue("exemplars/" + [n.id for n in store.notes() if n.id.endswith("AAAA0001")][0].split("/")[1], on=True)
    families.write(store, families.build(store))
    m = families.match(store, "Ile umów leasingowych zawarto w 2003r?")
    assert m is not None and m.values == {"rokum": "2003"}
    assert "WHERE rokum = '2003'" in m.program and "{{" not in m.program
    assert m.score >= families.MATCH_THRESHOLD
    # A question with the right value but another intent does not match.
    assert families.match(store, "Jakie umowy z 2003r mają najwyższą wartość?") is None
    # Extra conditions the family never saw block the replay even when half
    # the words are shared (the Ford question of the 11 September eval).
    assert families.match(store, "Ile umów zawartych zostało na pojazdy marki Ford z województwa "
                                 "śląskiego zostało zawartych w 2003r") is None
    assert families.match(store, "Ile umów leasingowych zostało proszę zawartych w 2003r?") is not None
    # A question without a year cannot fill the parameter.
    assert families.match(store, "Ile umów leasingowych zawarto?") is None


def test_family_goes_back_to_proposed_when_its_canonical_loses_confirmation(tmp_path):
    store = make(tmp_path)
    canonical = [n.id for n in store.notes() if n.id.endswith("AAAA0001")][0]
    store.mark_catalogue(canonical, on=True)
    families.write(store, families.build(store))
    assert store.get("families/zumowyl-count-rokum").status == "confirmed"
    note = store.get(canonical)
    note.status = "proposed"                 # what a review rewrite does
    note.catalogue = False
    from curie.memory.store import write_note
    write_note(note)
    store.refresh(force=True)
    families.write(store, families.build(store))
    fam = store.get("families/zumowyl-count-rokum")
    assert fam.status == "proposed" and not fam.catalogue
    assert fam.body.startswith("Set back to proposed")
    assert families.match(store, "Ile umów zawarto w 2003r?") is None


def test_family_note_survives_a_persons_edit(tmp_path):
    store = make(tmp_path)
    families.write(store, families.build(store))
    note = store.get("families/zumowyl-count-rokum")
    note.status = "confirmed"
    note.body = "Liczba umów zawartych w danym roku (ROKUM).\n\n```abap\n" + note.program + "\n```"
    note.aliases.append("How many agreements were concluded in {year}?")
    from curie.memory.store import write_note
    write_note(note)
    store.refresh(force=True)
    families.write(store, families.build(store))
    again = store.get("families/zumowyl-count-rokum")
    assert again.status == "confirmed"
    assert again.body.startswith("Liczba umów zawartych w danym roku (ROKUM).")
    assert "How many agreements were concluded in {year}?" in again.aliases


# -- the model as a router (plan 8.3) ------------------------------------------------ #

import asyncio  # noqa: E402


class RouterLLM:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    async def complete(self, messages, **kw):
        self.prompts.append(messages[1]["content"])
        return self.answer


def confirmed_family(tmp_path):
    store = make(tmp_path)
    first = [n.id for n in store.notes() if n.id.endswith("AAAA0001")][0]
    store.mark_catalogue(first, on=True)
    families.write(store, families.build(store))
    return store


def test_router_candidates_need_a_lexical_signal_and_fillable_parameters(tmp_path):
    store = confirmed_family(tmp_path)
    # A shared table name, or a word shared with an intent, and a year in the question.
    assert [n.id for n in families.candidates(store, "Policz kontrakty z 2003 w ZUMOWYL")] == ["families/zumowyl-count-rokum"]
    assert [n.id for n in families.candidates(store, "Policz kontrakty z 2003", tables=["ZUMOWYL"])] == ["families/zumowyl-count-rokum"]
    assert families.candidates(store, "Policz kontrakty z 2003") == []            # nothing in common
    assert families.candidates(store, "Ile umów zawarto w ZUMOWYL?") == []          # no year to fill


def test_routed_pick_is_admitted_only_with_values_from_the_question(tmp_path):
    store = confirmed_family(tmp_path)
    note = store.get("families/zumowyl-count-rokum")
    m = families.routed(note, {"rokum": "2003"}, "Policz kontrakty leasingowe z 2003 roku w ZUMOWYL")
    assert m is not None and m.routed and m.values == {"rokum": "2003"} and "rokum = '2003'" in m.program
    assert families.routed(note, {"rokum": "2009"}, "Policz kontrakty z 2003 w ZUMOWYL") is None   # invented
    assert families.routed(note, {}, "Policz kontrakty z 2003 w ZUMOWYL") is None
    assert families.routed(note, {"rokum": "ZUMOWYL"}, "Policz kontrakty z 2003 w ZUMOWYL") is None  # not typed


def test_route_asks_the_model_and_fails_closed(tmp_path):
    store = confirmed_family(tmp_path)
    q = "Policz kontrakty leasingowe z 2003 roku w ZUMOWYL"
    llm = RouterLLM('{"family": "families/zumowyl-count-rokum", "values": {"rokum": "2003"}}')
    m = asyncio.run(families.route(store, q, llm))
    assert m is not None and m.note.id == "families/zumowyl-count-rokum" and m.values == {"rokum": "2003"}
    assert "id: families/zumowyl-count-rokum" in llm.prompts[0] and "rokum (year, e.g. 2005)" in llm.prompts[0]
    assert asyncio.run(families.route(store, q, RouterLLM('{"family": null}'))) is None
    assert asyncio.run(families.route(store, q, RouterLLM('{"family": "families/other", "values": {}}'))) is None
    assert asyncio.run(families.route(store, q, RouterLLM("not json"))) is None
    # No candidates: the model is not even asked.
    quiet = RouterLLM('{"family": "families/zumowyl-count-rokum", "values": {"rokum": "2003"}}')
    assert asyncio.run(families.route(store, "Saldo konta 2003", quiet)) is None and quiet.prompts == []


def test_leaking_template_is_rebuilt_from_the_canonical_member(tmp_path):
    from curie.memory.store import write_note
    store = KnowledgeStore(tmp_path / "knowledge", refresh_every=0)
    program = ("REPORT z.\nDATA lv_rokum LIKE zreczne-rokum.\nDATA lv_nrum LIKE zreczne-nrum.\n"
               "lv_rokum = '2005'.\nlv_nrum = '0000339'.\n"
               "SELECT * FROM zreczne INTO TABLE lt WHERE rokum = lv_rokum AND nrum = lv_nrum.\n"
               "WRITE: / 'Faktury - umowa 339/2005'.")
    add_exemplar(store, "Pokaż dane faktury dla umowy 339/2005", program, "AAAA0009", catalogue=True)
    families.write(store, families.build(store))
    fam = [n for n in store.notes() if n.type == "family"][0]
    assert fam.status == "confirmed" and "{{" in fam.program and "339/" not in fam.program
    # Damage the template the way the older rule did.
    fam.program = fam.program.replace("{{rokum:text}}", "339/{{rokum:bare}}").replace("{{nrum:text}}", "339/{{rokum:bare}}")
    fam.body = fam.body.replace("{{rokum:text}}", "339/{{rokum:bare}}").replace("{{nrum:text}}", "339/{{rokum:bare}}")
    write_note(fam)
    store.refresh(force=True)
    assert "339/" in store.get(fam.id).program
    m = families.match(store, "Pokaż dane faktury dla umowy 28/2005")
    assert m is not None and "umowa 28/2005'" in m.program and "339" not in m.program
    repaired = store.get(fam.id)
    assert "339/" not in repaired.program and "339/" not in repaired.body and repaired.status == "confirmed"
    # A family whose canonical member is gone cannot be rebuilt: no replay.
    fam = store.get(fam.id)
    for hole in ("{{rokum:text}}", "{{nrum:text}}"):
        fam.program = fam.program.replace(hole, "339/{{rokum:bare}}")
        fam.body = fam.body.replace(hole, "339/{{rokum:bare}}")
    fam.family = "exemplars/gone"
    write_note(fam)
    store.refresh(force=True)
    assert families.match(store, "Pokaż dane faktury dla umowy 28/2005") is None
