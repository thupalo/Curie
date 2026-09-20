"""The dictionary index and the sap_find tool."""
from curie.memory import Knowledge, KnowledgeStore
from curie.memory import bundle as bundle_mod
from curie.memory import dictionary as dict_mod
from curie.sap_tools.read_table import FieldInfo
from tests.test_bundle import make


class FakeDdic:
    STRUCTURES = {
        "ZUMOWYL": [FieldInfo("ROKUM", key=True, datatype="NUMC", intlen=4, text="Rok umowy", rollname="ZROKUM"),
                    FieldInfo("NRUM", key=True, datatype="NUMC", intlen=7, text="Numer umowy"),
                    FieldInfo("KLIENT", datatype="CHAR", intlen=10, text="Numer klienta", checktable="ZKNA1"),
                    FieldInfo("STAN", datatype="NUMC", intlen=2, text="Stan umowy"),
                    FieldInfo("DATAUM", datatype="DATS", intlen=8, text="Data zawarcia umowy")],
        "ZUBEZP": [FieldInfo("ROKUM", key=True, datatype="NUMC", intlen=4, text="Rok umowy"),
                   FieldInfo("NRUM", key=True, datatype="NUMC", intlen=7, text="Numer umowy"),
                   FieldInfo("NRPOLISY", datatype="CHAR", intlen=20, text="Numer polisy"),
                   FieldInfo("DATAOD", datatype="DATS", intlen=8, text="Polisa ważna od")],
        "ZV_UM_KLI_PRZED": [FieldInfo("NRREJ", datatype="CHAR", intlen=10, text="Numer rejestracyjny"),
                            FieldInfo("VIN", datatype="CHAR", intlen=17, text="Numer VIN")],
    }

    def structure(self, name):
        if name not in self.STRUCTURES:
            raise KeyError(name)
        return {"name": name, "text": "", "fields": self.STRUCTURES[name]}


TEXTS = {"ZUMOWYL": {"text": "Umowy leasingowe", "text_en": "Lease agreements", "class": "TRANSP"},
         "ZUBEZP": {"text": "Polisy ubezpieczeniowe", "class": "TRANSP"},
         "ZV_UM_KLI_PRZED": {"text": "Umowa, klient, przedmiot", "class": "VIEW"}}


def build_index():
    return dict_mod.build(FakeDdic(), ["ZUMOWYL", "ZUBEZP", "ZV_UM_KLI_PRZED", "ZNOPE"], TEXTS, log_every=0)


def test_build_and_search():
    data = build_index()
    assert set(data["tables"]) == {"ZUMOWYL", "ZUBEZP", "ZV_UM_KLI_PRZED"}     # ZNOPE unreadable: left out
    index = dict_mod.Index(data)
    hit = index.search("polisa ubezpieczeniowa")
    assert hit["tables"][0]["name"] == "ZUBEZP"
    names = [f["name"] for f in hit["tables"][0]["fields"]]
    assert "NRPOLISY" in names and "DATAOD" in names and "ROKUM" in names   # matched, then keys
    hit = index.search("numer rejestracyjny pojazdu")
    assert hit["tables"][0]["name"] == "ZV_UM_KLI_PRZED"
    assert hit["tables"][0]["fields"][0]["name"] == "NRREJ"
    # inflection and diacritics: "polisy ważnej" finds DATAOD by prefix
    assert any(t["name"] == "ZUBEZP" for t in index.search("od kiedy polisy są ważne")["tables"])
    # identifiers: a field name finds every table that has it
    hit = index.search("ROKUM", kind="field")
    assert {t["name"] for t in hit["tables"]} == {"ZUMOWYL", "ZUBEZP"}
    assert index.search("xyzzy")["tables"] == [] and "no table" in index.search("xyzzy")["note"]
    line = index.render_table("ZUMOWYL", max_fields=2)
    assert line.startswith("table ZUMOWYL (Umowy leasingowe): keys ROKUM, NRUM; fields KLIENT (Numer klienta), STAN")
    assert "5 fields in all" in line


def test_usage_ranks_copies_below_and_used_fields_first():
    data = build_index()
    data["tables"]["ZUBEZP_A"] = dict(data["tables"]["ZUBEZP"], text="Ubezpieczenia (archiwum)")
    data["tables"]["ZUMOWYL_KOPIA2"] = dict(data["tables"]["ZUMOWYL"], text="Kopia danych z 2005")
    data["usage"] = {"ZUMOWYL": 30, "ZUBEZP": 4}
    data["field_usage"] = {"ZUMOWYL": {"DATAUM": 12, "STAN": 9}}
    index = dict_mod.Index(data)
    hit = index.search("polisa")
    assert hit["tables"][0]["name"] == "ZUBEZP" and hit["tables"][0]["variants"] == ["ZUBEZP_A"]
    assert all(t["name"] != "ZUBEZP_A" for t in hit["tables"])
    hit = index.search("umowy leasingowe")
    assert hit["tables"][0]["name"] == "ZUMOWYL" and "ZUMOWYL_KOPIA2" in hit["tables"][0]["variants"]
    line = index.render_table("ZUMOWYL", max_fields=2)
    assert "fields DATAUM (Data zawarcia umowy), STAN (Stan umowy)" in line
    used = [f["name"] for f in hit["tables"][0]["fields"]]
    assert "DATAUM" in used and "STAN" in used


def test_usage_from_notes_and_archive(tmp_path):
    from curie.archive import Archive
    from tests.test_archive import new_format_session, write_log
    from tests.test_families import make as make_store

    store = make_store(tmp_path)
    root = tmp_path / "logs"
    write_log(root / "2026-09-11" / "AAAA1111.jsonl", new_format_session())
    archive = Archive(tmp_path / "archive" / "curie.db")
    archive.import_dir(root)
    tables, fields = dict_mod.usage_from(archive, store)
    archive.close()
    assert tables["ZUMOWYL"] >= 4                 # three exemplars, one structure call, one run
    assert fields["ZUMOWYL"]["ROKUM"] >= 3


def test_knowledge_offers_find_and_neighbourhood(tmp_path):
    root, store = make(tmp_path)
    knowledge = Knowledge(store, root / ".build", bundle_check_every=0)
    assert knowledge.find("polisa")["indexed_tables"] == 0
    b = bundle_mod.Bundle(dictionary=build_index(), manifest=knowledge.bundle.manifest)
    bundle_mod.save(b, root / ".build", parts=("dictionary.json",))
    knowledge.reload_bundle()
    assert len(knowledge.dictionary) == 3
    assert knowledge.find("polisa")["tables"][0]["name"] == "ZUBEZP"
    ctx = knowledge.context_for("Ile aktywnych umów leasingowych?", [], "")
    assert ctx.dictionary == ["ZUMOWYL"]
    assert "From the data dictionary" in ctx.text and "keys ROKUM, NRUM" in ctx.text


def test_toolbox_offers_sap_find_only_with_an_index(tmp_path):
    import asyncio
    from curie.llm.tools import ToolBox

    root, store = make(tmp_path)
    knowledge = Knowledge(store, root / ".build", bundle_check_every=0)
    box = ToolBox(None, None, None, None, None, knowledge)
    assert "sap_find" not in [s["function"]["name"] for s in box.schemas]
    b = bundle_mod.Bundle(dictionary=build_index(), manifest=knowledge.bundle.manifest)
    bundle_mod.save(b, root / ".build", parts=("dictionary.json",))
    knowledge.reload_bundle()
    assert "sap_find" in [s["function"]["name"] for s in box.schemas]
    ok, result, summary = asyncio.run(box.dispatch("sap_find", {"query": "numer VIN"}))
    assert ok and result["tables"][0]["name"] == "ZV_UM_KLI_PRZED" and summary.endswith("table(s)")
    assert box.describe("sap_find", {"query": "x"}) == "Searching the dictionary for x"
