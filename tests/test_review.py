"""The knowledge review job: facts, follow-up questions, duplicates, apply."""
import asyncio
import json

from curie import config
from curie.memory.review import DictionaryCheck, KnowledgeReviewer, content_hash, similar_notes
from tests.test_knowledge import make_store

PROGRAM = ("REPORT zai_x.\nDATA lv_n TYPE i.\nSELECT COUNT(*) FROM zumowyl INTO lv_n\n"
           "  WHERE rokum = '2004'.\nWRITE: / 'Umowy 2004:', lv_n.")


class ReviewLLM:
    """Verdict per note id substring; default ok. Records the prompts."""

    def __init__(self, verdicts=None, raw=None):
        self.verdicts = verdicts or {}
        self.raw = raw
        self.prompts = []

    async def complete(self, messages, **kw):
        user = messages[1]["content"]
        self.prompts.append(user)
        if self.raw is not None:
            return self.raw
        note_id = user.split()[1]
        for key, verdict in self.verdicts.items():
            if key in note_id:
                return json.dumps(verdict)
        return json.dumps({"verdict": "ok", "problems": [], "question": None,
                           "duplicate_of": None, "confidence": 0.9})


class FakeSap:
    """Enough of SapClient for DictionaryCheck: DD02L and DD03L rows."""

    tables = {"ZUMOWYL": {"MANDT", "ROKUM", "NRUM", "STAN"}, "ZKLIENT": {"MANDT", "KUNNR"}}

    async def run(self, fn, *args):
        _sap, table, fields, where, n = args
        name = where.split("'")[1]
        if table == "DD02L":
            return ["TABNAME"], ([[name]] if name in self.tables else [])
        return ["FIELDNAME"], [[f] for f in sorted(self.tables.get(name, ()))]


def test_pending_similar_and_dictionary_check(tmp_path):
    store = make_store(tmp_path)
    a = store.add_exemplar("Ile umów zawarto w 2004 roku?", PROGRAM, "T", "00000001")
    b = store.add_exemplar("Wygeneruj jeszcze raz poprawiając problem", PROGRAM, "T", "00000002")
    hint = store.add_hint("Rok umowy to ROKUM w tabeli ZUMOWYL, a klient to KUNNRX w ZUMOWYL.", "T",
                          ["ZUMOWYL"])
    bad = store.add_hint("Dane są w tabeli ZNIEMA.", "T", ["ZNIEMA"])
    reviewer = KnowledgeReviewer(store, ReviewLLM())
    ids = [n.id for n in reviewer.pending()]
    assert ids[:2] == sorted([a.id, b.id]) and hint.id in ids and "tables/ZUMOWYL" in ids
    assert [n.id for n in similar_notes(store, a)] == [b.id]

    async def checks():
        ddic = DictionaryCheck(FakeSap())
        assert await ddic.check(store.get(a.id)) == []
        assert await ddic.check(store.get(hint.id)) == ["field KUNNRX is not in ZUMOWYL"]
        assert await ddic.check(store.get(bad.id)) == ["table ZNIEMA does not exist in the dictionary"]

    asyncio.run(checks())


def test_run_flags_reports_and_skips_reviewed_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KNOWLEDGE_REVIEW_MAX", 50)
    store = make_store(tmp_path)
    good = store.add_exemplar("Ile umów zawarto w 2004 roku?", PROGRAM, "T", "00000001")
    follow = store.add_exemplar("Wygeneruj jeszcze raz poprawiając problem", PROGRAM, "T", "00000002")
    dup = store.add_exemplar("Policz umowy z roku 2004", PROGRAM, "T", "00000003")
    bad = store.add_hint("Dane są w tabeli ZNIEMA.", "T", ["ZNIEMA"])
    llm = ReviewLLM({
        "00000002": {"verdict": "fix", "problems": ["question is a follow-up"],
                     "question": "Ile umów zawarto w 2004 roku?", "confidence": 0.8},
        "00000003": {"verdict": "duplicate", "problems": [], "duplicate_of": good.id},
        bad.id.split("/")[1]: {"verdict": "retire", "problems": ["no such table"]},
    })
    reviewer = KnowledgeReviewer(store, llm, FakeSap())

    report = asyncio.run(reviewer.run())
    assert report["reviewed"] == len(store.notes()) and report["unreviewed"] == 0
    flagged = {f["id"]: f for f in report["flags"]}
    assert flagged[follow.id]["verdict"] == "fix"
    assert flagged[follow.id]["question"] == "Ile umów zawarto w 2004 roku?"
    assert flagged[dup.id]["verdict"] == "duplicate" and flagged[dup.id]["duplicate_of"] == good.id
    assert flagged[bad.id]["verdict"] == "retire"
    assert flagged[bad.id]["problems"][0] == "table ZNIEMA does not exist in the dictionary"
    assert good.id not in flagged
    # the heuristic flag reached the model for the follow-up question
    assert any("reads like a follow-up" in p for p in llm.prompts)
    # nothing was changed without apply; verdicts sit in the frontmatter
    assert store.get(follow.id).question.startswith("Wygeneruj") and store.get(bad.id).status == "confirmed"
    assert store.get(follow.id).review["verdict"] == "fix"
    assert store.get(good.id).review["verdict"] == "ok"
    report_text = (tmp_path / "REVIEW.md").read_text(encoding="utf-8")
    assert f"[[{follow.id}]]" in report_text and "suggested question:" in report_text
    assert "REVIEW" not in [n.id for n in store.notes()]      # the report is not a note
    # a second run has nothing to do; editing a note makes it pending again
    assert reviewer.pending() == []
    n = store.get(good.id)
    n.body += "\nedited"
    from curie.memory.store import write_note
    write_note(n)
    store.refresh(force=True)
    assert [x.id for x in reviewer.pending()] == [good.id]
    assert content_hash(store.get(good.id)) != n.review["hash"]


def test_apply_rewrites_retires_and_keeps_the_better_duplicate(tmp_path):
    store = make_store(tmp_path)
    good = store.add_exemplar("Ile umów zawarto w 2004 roku?", PROGRAM, "T", "00000001")
    store.record([good.id], success=True)
    follow = store.add_exemplar("Wygeneruj jeszcze raz poprawiając problem", PROGRAM, "T", "00000002")
    store.mark_catalogue(follow.id)
    dup = store.add_exemplar("Policz umowy z roku 2004", PROGRAM, "T", "00000003")
    bad = store.add_hint("Dane są w tabeli ZNIEMA.", "T", ["ZNIEMA"])
    llm = ReviewLLM({
        "00000002": {"verdict": "fix", "problems": ["follow-up"], "question": "Ile umów zawarto w 2004 roku?"},
        "00000003": {"verdict": "duplicate", "duplicate_of": good.id},
        bad.id.split("/")[1]: {"verdict": "retire", "problems": ["no such table"]},
    })
    report = asyncio.run(KnowledgeReviewer(store, llm).run(apply=True))
    assert report["applied"] == 3
    fixed = store.get(follow.id)
    assert fixed.question == "Ile umów zawarto w 2004 roku?" and fixed.status == "proposed"
    assert fixed.catalogue is False and fixed.refinements == ["was: Wygeneruj jeszcze raz poprawiając problem"]
    assert store.get(dup.id).status == "retired" and store.get(good.id).status == "confirmed"
    assert store.get(bad.id).status == "retired" and "Retired by the knowledge review" in store.get(bad.id).body


def test_apply_later_acts_on_earlier_verdicts(tmp_path):
    """A run without --apply stores the verdict; a later --apply run applies
    it without asking the model again, and the report keeps listing it."""
    store = make_store(tmp_path)
    a = store.add_exemplar("Napisz program liczący umowy z 2004", PROGRAM, "T", "00000001")
    b = store.add_exemplar("Ile umów zawarto w 2005 roku?", PROGRAM.replace("2004", "2005"), "T", "00000002")
    b.tables = ["LT_K", "ZUMOWYL"]           # the old table detection's leftover
    from curie.memory.store import write_note
    write_note(b)
    store.refresh(force=True)
    llm = ReviewLLM({"00000001": {"verdict": "fix", "problems": ["a programming request"],
                                  "question": "Ile umów zawarto w 2004 roku?", "confidence": 0.8}})
    reviewer = KnowledgeReviewer(store, llm, FakeSap())
    first = asyncio.run(reviewer.run(apply=False))
    assert first["applied"] == 0 and {f["id"] for f in first["flags"]} >= {a.id, b.id}
    assert store.get(a.id).question.startswith("Napisz")
    calls = len(llm.prompts)
    # Nothing pending, but the flags are still open: reported, not lost.
    second = asyncio.run(reviewer.run(apply=False))
    assert second["reviewed"] == 0 and second["outstanding"] == 2 and len(llm.prompts) == calls
    text = (store.root / "REVIEW.md").read_text(encoding="utf-8")
    assert "(from an earlier run)" in text and "2 from earlier runs, still open" in text
    # --apply on a later run acts on the stored verdicts.
    third = asyncio.run(reviewer.run(apply=True))
    assert third["applied"] == 2 and len(llm.prompts) == calls
    assert store.get(a.id).question == "Ile umów zawarto w 2004 roku?"
    assert store.get(a.id).review.get("applied")
    assert store.get(b.id).tables == ["ZUMOWYL"]
    # Applied verdicts are not applied again; a changed note is reviewed afresh.
    fourth = asyncio.run(reviewer.run(apply=True))
    # a's question changed: reviewed again, the same rewrite is not applied twice;
    # b's tables are not part of the content hash: its applied verdict rests.
    assert fourth["applied"] == 0 and fourth["reviewed"] == 1 and fourth["outstanding"] == 0
    # A second, different rewrite of the same note is reported, never applied:
    # one automatic rewrite per note, then a person decides.
    llm.verdicts["00000001"]["question"] = "Ile umów leasingowych zawarto w 2004 roku?"
    a2 = store.get(a.id)
    a2.body += "\nchanged"
    write_note(a2)
    store.refresh(force=True)
    fifth = asyncio.run(reviewer.run(apply=True))
    assert fifth["reviewed"] == 1 and fifth["applied"] == 0
    assert store.get(a.id).question == "Ile umów zawarto w 2004 roku?"


def test_fail_closed_on_bad_verdicts(tmp_path):
    store = make_store(tmp_path)
    ex = store.add_exemplar("Ile umów zawarto w 2004 roku?", PROGRAM, "T", "00000001")
    reviewer = KnowledgeReviewer(store, ReviewLLM(raw="I think it is fine."))
    report = asyncio.run(reviewer.run())
    assert report["reviewed"] == 0 and report["unreviewed"] == len(store.notes())
    assert store.get(ex.id).review is None and [n.id for n in reviewer.pending()]
    # a duplicate verdict naming a note that is not similar is downgraded
    reviewer = KnowledgeReviewer(store, ReviewLLM(raw='{"verdict": "duplicate", "duplicate_of": "tables/ZUMOWYL"}'))
    report = asyncio.run(reviewer.run())
    assert report["flags"] == [] and store.get(ex.id).review["verdict"] == "ok"
