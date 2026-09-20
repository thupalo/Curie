"""The Knowledge facade and the compiled bundle."""
import json
import time

from curie.memory import Knowledge, KnowledgeStore
from curie.memory import bundle as bundle_mod
from tests.test_knowledge import CONCEPT_NOTE, TABLE_NOTE


def make(tmp_path):
    root = tmp_path / "knowledge"
    (root / "tables").mkdir(parents=True)
    (root / "concepts").mkdir()
    (root / "tables" / "ZUMOWYL.md").write_text(TABLE_NOTE, encoding="utf-8")
    (root / "concepts" / "active-agreement.md").write_text(CONCEPT_NOTE, encoding="utf-8")
    store = KnowledgeStore(root, refresh_every=0)
    return root, store


def test_bundle_is_built_saved_and_reused(tmp_path):
    root, store = make(tmp_path)
    knowledge = Knowledge(store, root / ".build", bundle_check_every=0)
    build = root / ".build"
    assert (build / "manifest.json").exists() and (build / "index.json").exists()
    index = json.loads((build / "index.json").read_text(encoding="utf-8"))
    assert "ZUMOWYL" in index["identifiers"] and "leasi" in index["prefixes"]
    assert knowledge.bundle.summary()["notes"] == 2
    built = knowledge.bundle.manifest["built"]
    # Unchanged notes: the bundle on disk is reused, not rebuilt.
    again = Knowledge(store, build, bundle_check_every=0)
    assert again.bundle.manifest["built"] == built
    # A new note makes the lexical parts stale; a derived part survives the rebuild.
    (build / "glossary.json").write_text(json.dumps({"polisa": {"tables": ["ZUBEZP"]}}), encoding="utf-8")
    again.reload_bundle()
    assert again.bundle.glossary == {"polisa": {"tables": ["ZUBEZP"]}}
    time.sleep(0.02)
    (root / "concepts" / "policy.md").write_text(
        "---\ntype: concept\nname: policy\naliases: [polisa]\ntables: [ZUBEZP]\n---\nInsurance policies.\n",
        encoding="utf-8")
    store.refresh(force=True)
    again.ensure_bundle(force=True)
    assert again.bundle.summary()["notes"] == 3
    assert again.bundle.glossary == {"polisa": {"tables": ["ZUBEZP"]}}
    assert again.bundle.manifest["built"] > built


def test_context_for_reports_what_went_in(tmp_path):
    root, store = make(tmp_path)
    knowledge = Knowledge(store, root / ".build", bundle_check_every=0)
    ctx = knowledge.context_for("Ile aktywnych umów leasingowych?", [], "")
    assert "tables/ZUMOWYL" in ctx.notes and "concepts/active-agreement" in ctx.notes
    assert "Facts about this system" in ctx.text and not ctx.card
    # A system card written by a person goes in front of everything.
    (root / "CARD.md").write_text("R/3 4.5B leasing system; agreements in ZUMOWYL.", encoding="utf-8")
    ctx = knowledge.context_for("Ile aktywnych umów?", [], "")
    assert ctx.card and ctx.text.startswith("About this system (the system card):\nR/3 4.5B leasing")
    assert len(store) == 2                      # CARD.md is not a note


def test_glossary_expands_the_tables(tmp_path):
    root, store = make(tmp_path)
    knowledge = Knowledge(store, root / ".build", bundle_check_every=0)
    bundle_mod.save(bundle_mod.Bundle(glossary={"polisa ubezpieczeniowa": {"tables": ["ZUMOWYL"]}},
                                      manifest=knowledge.bundle.manifest), root / ".build",
                    parts=("glossary.json",))
    knowledge.reload_bundle()
    assert knowledge.glossary_tables("Jakie polisy ubezpieczeniowe ma klient?") == ["ZUMOWYL"]
    ctx = knowledge.context_for("Jakie polisy ubezpieczeniowe ma klient?", [], "")
    assert ctx.glossary_tables == ["ZUMOWYL"] and "tables/ZUMOWYL" in ctx.notes


def test_facade_delegates_to_the_store(tmp_path):
    root, store = make(tmp_path)
    knowledge = Knowledge(store, root / ".build", bundle_check_every=0)
    hint = knowledge.add_hint("Rok umowy to ROKUM.", "T", ["ZUMOWYL"])
    assert knowledge.get(hint.id) is not None and len(knowledge) == 3
    knowledge.table_exists = lambda name: name == "ZUMOWYL"
    assert store.table_exists("ZUMOWYL") and not store.table_exists("ZAKUPU")
    assert knowledge.match_family("Ile umów w 2004?") is None
    found = knowledge.search("leasing")
    assert any(n.id == "tables/ZUMOWYL" for n in found)


def test_frontmatter_extensions_round_trip(tmp_path):
    root, store = make(tmp_path)
    from curie.memory.store import parse_note, write_note
    note = store.get("tables/ZUMOWYL")
    note.facts = [{"subject": "ZUMOWYL.KLIENT", "predicate": "joins", "object": "ZKNA1.KUNNR"}]
    note.family = "count-agreements"
    note.params = {"year": {"type": "GJAHR"}}
    note.trigger = "period missing"
    write_note(note)
    again = parse_note(note.path, root)
    assert again.facts[0]["predicate"] == "joins" and again.family == "count-agreements"
    assert again.params == {"year": {"type": "GJAHR"}} and again.trigger == "period missing"
