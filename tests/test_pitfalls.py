"""P7: the pitfall consolidator (triggers, merges, counter-evidence, syntax clusters)."""
import json

from curie.memory import pitfalls
from curie.memory.store import KnowledgeStore


class FakeArchive:
    def __init__(self, events, calls):
        self.events, self.calls = events, calls

    def query(self, sql, args=()):
        if "FROM events" in sql:
            return [{"episode_id": ep, "data": json.dumps(d)} for ep, d in self.events]
        if "FROM calls" in sql:
            needle = args[0].strip("%")
            return [{"episode_id": ep} for ep, notes in self.calls if needle in notes]
        raise AssertionError(sql)


def make(tmp_path):
    store = KnowledgeStore(tmp_path / "kb", refresh_every=0)
    a = store.add_insight("End every statement before ELSE and ENDIF with a period.", source="curie:syntax",
                          keywords=["ENDIF"])
    b = store.add_insight("Every statement before ENDIF and ELSE must end with a period", source="curie:syntax",
                          keywords=["period"])
    c = store.add_insight("Place UP TO n ROWS before the WHERE clause on this release.", source="curie:syntax",
                          keywords=["UP"], tables=["ZUMOWYL"])
    a.successes = 2
    from curie.memory.store import write_note
    write_note(a)
    store.refresh(force=True)
    return store, a, b, c


def test_syntax_clusters_count_and_mark_repaired_words():
    archive = FakeArchive([("E1", {"word": "ENDIF", "message": 'Statement before "ENDIF ..." not concluded (period missing).'}),
                           ("E1", {"word": "ENDIF", "message": 'Statement before "ENDIF ..." not concluded (period missing).'}),
                           ("E2", {"word": "TOWAZ", "message": 'Unknown column name "TOWAZ".'}),
                           ("E3", {"word": "TOWAZ", "message": 'Unknown column name "TOWAZ".'}),
                           ("E4", {"word": "UP", "message": 'Incorrect expression "UP" in logical condition.'})], [])
    clusters = pitfalls.syntax_clusters(archive)
    assert [(c["word"], c["count"], c["episodes"], c["repaired"]) for c in clusters] == [
        ("ENDIF", 2, 1, True), ("TOWAZ", 2, 2, False), ("UP", 1, 1, True)]
    assert pitfalls.syntax_clusters(None) == []


def test_consolidate_sets_triggers_merges_duplicates_and_counts_counter_evidence(tmp_path):
    store, a, b, c = make(tmp_path)
    archive = FakeArchive(
        [("E1", {"word": "ENDIF", "message": 'Statement before "ENDIF ..." not concluded (period missing).'}),
         ("E2", {"word": "ENDIF", "message": 'Statement before "ENDIF ..." not concluded (period missing).'}),
         ("E3", {"word": "TOWAZ", "message": 'Unknown column name "TOWAZ".'}),
         ("E4", {"word": "TOWAZ", "message": 'Unknown column name "TOWAZ".'})],
        [("E1", json.dumps([a.id])), ("E5", json.dumps([a.id])), ("E2", json.dumps([c.id]))])
    report = pitfalls.consolidate(store, archive)
    assert report["merged"] == [(b.id, a.id)]
    kept, dropped = store.get(a.id), store.get(b.id)
    assert dropped.status == "retired" and dropped.superseded_by == a.id
    assert kept.trigger.startswith("ENDIF: Statement before") and kept.uses == 0 and kept.successes == 2
    assert kept.counter_evidence == 1                     # in the prompt of E1, which failed with ENDIF anyway
    assert store.get(c.id).trigger == "" and report["triggers"] == 2      # a and b, before the merge
    assert report["insights"] == 2 and report["counter_evidence"] == 1
    assert [(r["word"], r["episodes"]) for r in report["rule_candidates"]] == [("TOWAZ", 2)]
    part = report["bundle"]
    assert [p["id"] for p in part["pitfalls"]] == [a.id, c.id]
    assert part["pitfalls"][0]["counter_evidence"] == 1 and "episode_ids" not in part["syntax"][0]
    path = pitfalls.write_part(part, tmp_path / "build")
    assert json.loads(path.read_text(encoding="utf-8"))["syntax"][0]["word"] == "ENDIF"
    # Idempotent: a second run changes nothing.
    again = pitfalls.consolidate(store, archive)
    assert again["merged"] == [] and again["triggers"] == 0 and again["counter_evidence"] == 0
