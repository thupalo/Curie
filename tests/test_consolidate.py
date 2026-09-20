"""The consolidation worker: families, glossary, card, bundle."""
import asyncio
import json

from curie.memory import bundle as bundle_mod
from curie.memory.consolidate import Consolidator
from curie.memory.glossary import mine, question_terms
from tests.test_families import make


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def complete(self, messages, model=None, temperature=0.0, json_mode=False, max_tokens=None):
        self.calls.append(messages[-1]["content"])
        return self.answers.pop(0) if self.answers else ""


def test_glossary_mining():
    pairs = [("Ile umów zawarto w 2004?", ["ZUMOWYL"]),
             ("Ile umów leasingowych zawarto w 2005?", ["ZUMOWYL"]),
             ("Wylistuj umowy z województwa podlaskiego", ["ZUMOWYL", "ZKNA1"]),
             ("Jakie polisy ma umowa 100/2005?", ["ZUBEZP", "ZUMOWYL"]),
             ("Jakie polisy wystawiono dla pojazdu CB83602?", ["ZUBEZP"]),
             ("Pokaż zawartość tabeli T000", ["T000"])]
    terms = mine(pairs)
    assert terms["umów"[:5]]["tables"] == ["ZUMOWYL"] and terms["umów"[:5]]["support"] >= 2
    assert terms["polis"]["tables"] == ["ZUBEZP"]
    assert "pokaż"[:5] not in terms and "zawar" not in terms or terms.get("zawar", {}).get("tables") == ["ZUMOWYL"]
    assert "ile" not in question_terms("Ile umów zawarto?")


def test_worker_without_a_model(tmp_path):
    store = make(tmp_path)
    worker = Consolidator(store, tmp_path / "knowledge" / ".build", llm=None, archive=None)
    report = asyncio.run(worker.run())
    assert report["families"]["written"] == ["families/zumowyl-count-rokum"]
    assert report["card"] == {"skipped": "no model"}
    assert report["glossary"]["terms"] >= 1
    bundle = bundle_mod.load(tmp_path / "knowledge" / ".build")
    assert "umów"[:5] in bundle.glossary and bundle.index["notes"]["families/zumowyl-count-rokum"]["type"] == "family"
    assert (tmp_path / "knowledge" / ".build" / "consolidate.json").exists()


def test_worker_with_a_model_names_families_and_proposes_a_card(tmp_path):
    store = make(tmp_path)
    llm = FakeLLM([
        json.dumps({"intent_pl": "Ile umów zawarto w {year}?",
                    "intent_en": "How many agreements were concluded in {year}?",
                    "description": "Counts ZUMOWYL rows whose ROKUM is the given year."}),
        "**TC0 leasing system.** Agreements live in ZUMOWYL (one row per agreement, keys ROKUM/NRUM); "
        "users write an agreement as 100/2005. Report families: count of agreements per year.",
    ])
    worker = Consolidator(store, tmp_path / "knowledge" / ".build", llm=llm, archive=None)
    report = asyncio.run(worker.run())
    note = store.get("families/zumowyl-count-rokum")
    assert "How many agreements were concluded in {year}?" in note.aliases
    assert note.body.startswith("Counts ZUMOWYL rows whose ROKUM is the given year.")
    assert "Members:" in note.body and "{{rokum}}" in note.program
    assert report["families"]["named"] == 1
    build = tmp_path / "knowledge" / ".build"
    assert (build / "card.proposed.md").exists() and not (build / "card.md").exists()
    assert report["card"]["applied"] is False
    # Applied: the card is in the bundle and reaches the prompt.
    llm.answers = [json.dumps({"intent_pl": "x", "intent_en": "y", "description": "z"}),
                   "**TC0 leasing system.** The card, applied this time; long enough to be accepted."]
    report = asyncio.run(Consolidator(store, build, llm=llm, archive=None).run(apply_card=True))
    assert report["card"]["applied"] and (build / "card.md").exists()
    from curie.memory import Knowledge
    knowledge = Knowledge(store, build, bundle_check_every=0)
    assert knowledge.context_for("Ile umów w 2004?", [], "").card


def test_model_intents_are_rejected_without_placeholders(tmp_path):
    store = make(tmp_path)
    llm = FakeLLM([json.dumps({"intent_pl": "Ile umów zawarto?", "intent_en": "How many?",
                               "description": "d"})])
    worker = Consolidator(store, tmp_path / "knowledge" / ".build", llm=llm, archive=None)
    report = asyncio.run(worker.run())
    assert report["families"]["named"] == 0
    note = store.get("families/zumowyl-count-rokum")
    assert note.aliases == ["Ile umów zawarto w {year}?", "Ile umów leasingowych zostało zawartych w {year}?"]
