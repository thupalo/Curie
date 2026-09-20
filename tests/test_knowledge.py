"""The knowledge store, the chat commands and exemplar capture."""
import asyncio

from curie import config
from curie.llm.abap_flow import AbapFlow
from curie.llm.orchestrator import Orchestrator
from curie.memory.store import KnowledgeStore, program_tables, signature
from curie.sessions.manager import SessionManager
from tests.test_orchestrator import ATTRS, FakeExecutor, FakeLLM, FakeToolBox, drain

TABLE_NOTE = """---
type: table
name: ZUMOWYL
aliases: [umowa leasingowa, umowy leasingowe, leasing agreement]
keywords: [leasing]
status: confirmed
source: user:THUPALO
created: 2026-09-06
---
Lease agreements live in ZUMOWYL. See [[active-agreement]].
"""

CONCEPT_NOTE = """---
type: concept
name: active-agreement
aliases: [aktywna umowa, active agreement]
tables: [ZUMOWYL]
status: confirmed
source: user:THUPALO
created: 2026-09-06
---
Active means STATUS between 14 and 19.
"""

RETIRED_NOTE = """---
type: concept
name: old
aliases: [leasing]
status: retired
---
Outdated.
"""


def make_store(tmp_path):
    (tmp_path / "tables").mkdir()
    (tmp_path / "concepts").mkdir()
    (tmp_path / "tables" / "ZUMOWYL.md").write_text(TABLE_NOTE, encoding="utf-8")
    (tmp_path / "concepts" / "active-agreement.md").write_text(CONCEPT_NOTE, encoding="utf-8")
    (tmp_path / "concepts" / "old.md").write_text(RETIRED_NOTE, encoding="utf-8")
    return KnowledgeStore(tmp_path, refresh_every=0)


def test_parse_and_retrieve_by_name_alias_and_link(tmp_path):
    store = make_store(tmp_path)
    assert len(store) == 2                      # the retired note is not counted
    note = store.get("tables/ZUMOWYL")
    assert note.tables == ["ZUMOWYL"] and note.links == ["active-agreement"]

    # table name in the question
    notes, ex = store.retrieve("Ile wierszy ma tabela zumowyl?")
    assert [n.id for n in notes][0] == "tables/ZUMOWYL"
    assert "concepts/active-agreement" in [n.id for n in notes]   # via tables + wikilink
    # Polish inflection of an alias
    notes, _ = store.retrieve("Podaj liczbę umów leasingowych")
    assert notes and notes[0].id == "tables/ZUMOWYL"
    # tables from tool calls
    notes, _ = store.retrieve("how many rows?", tables=["ZUMOWYL"])
    assert notes and notes[0].id == "tables/ZUMOWYL"
    # nothing relevant
    assert store.retrieve("Who are you?") == ([], [])
    # retired notes never come back even on an exact alias
    assert all(n.id != "concepts/old" for n in store.retrieve("leasing")[0])


def test_render_separates_facts_and_hints(tmp_path):
    store = make_store(tmp_path)
    hint = store.add_hint("Umowy wypowiedziane mają STATUS 21.", "THUPALO", ["ZUMOWYL"])
    assert hint.id.startswith("hints/") and hint.status == "confirmed"
    assert hint.tables == ["ZUMOWYL"] and hint.source == "user:THUPALO"
    notes, ex = store.retrieve("status umów w ZUMOWYL")
    text = store.render(notes, ex)
    assert "Facts about this system" in text and "Hints users gave" in text
    assert "(THUPALO, " in text and "STATUS 21" in text
    # retire and it disappears
    assert store.retire(hint.id).status == "retired"
    assert hint.id not in [n.id for n in store.retrieve("status umów w ZUMOWYL")[0]]
    assert store.retire("nope") is None


def test_exemplar_capture_dedupe_and_governance(tmp_path):
    store = make_store(tmp_path)
    prog = "REPORT zai_x.\nSELECT COUNT(*) FROM zumowyl INTO lv_n\n  WHERE status BETWEEN '14' AND '19'.\nWRITE lv_n."
    n = store.add_exemplar("Ile jest aktywnych umów leasingowych?", prog, "THUPALO", "AB12CD34")
    assert n is not None and n.type == "exemplar" and n.tables == ["ZUMOWYL"]
    assert n.program.startswith("REPORT zai_x.")
    # a rephrasing with the same words is a duplicate
    assert store.add_exemplar("ile aktywnych umów leasingowych jest?", prog, "X", "00000001") is None
    notes, ex = store.retrieve("Ile jest aktywnych umów leasingowych w tym roku?")
    assert ex and ex[0].id == n.id
    rendered = store.render(notes, ex)
    assert "Programs confirmed earlier" in rendered and "```abap" in rendered
    # two failures retire it
    store.record([n.id], success=False)
    assert store.get(n.id).status == "confirmed"
    store.record([n.id], success=False)
    assert store.get(n.id).status == "retired" and store.get(n.id).uses == 2
    assert program_tables("SELECT * FROM mara JOIN marc ON ...") == ["MARA", "MARC"]
    assert signature("Ile umów?") == signature("ile UMÓW")


def test_hot_reload_picks_up_edits(tmp_path):
    store = make_store(tmp_path)
    (tmp_path / "concepts" / "new.md").write_text(
        "---\ntype: concept\nname: mpk\naliases: [centrum kosztów]\n---\nMPK is the cost centre.\n",
        encoding="utf-8")
    notes, _ = store.retrieve("jakie centrum kosztów")
    assert [n.id for n in notes] == ["concepts/new"]


def setup(tmp_path, llm, knowledge, executor=None):
    mgr = SessionManager(public_url="http://c", log_dir=str(tmp_path / "logs"))
    mgr.bind_loop(asyncio.get_running_loop())
    orch = Orchestrator(mgr, llm, FakeToolBox(), knowledge)
    flow = AbapFlow(mgr, executor or FakeExecutor(), llm, orch, knowledge)
    orch.flow = flow
    session = mgr.open_from_rfc(ATTRS, "")
    mgr.touch_browser(session)
    q = mgr.subscribe(session)
    drain(q)
    return mgr, orch, flow, session, q


def test_commands_remember_knowledge_forget(tmp_path):
    store = make_store(tmp_path / "kb") if (tmp_path / "kb").mkdir() is None else None

    async def scenario():
        mgr, orch, flow, session, q = setup(tmp_path, FakeLLM([]), store)
        session.last_prompt = "ile umów leasingowych"
        session.tables_used.add("ZUMOWYL")        # as if a tool had read it
        assert orch.command(session, "/remember ZUMOWYL-KLIENT is the customer number") == "remember"
        texts = [d["text"] for e, d in drain(q) if e == "notice"]
        assert texts and texts[0].startswith("Remembered as hints/")
        hint_id = texts[0].split()[2]
        assert session.messages[-1]["content"].startswith("[The user asked Curie to remember")
        assert orch.command(session, "/knowledge") == "knowledge"
        listing = [d["text"] for e, d in drain(q) if e == "notice"][0]
        assert "tables/ZUMOWYL" in listing and hint_id in listing
        assert orch.command(session, f"/forget {hint_id}") == "forget"
        assert store.get(hint_id).status == "retired"
        assert orch.command(session, "/forget") == "forget"       # usage notice, no crash
        assert orch.command(session, "plain question") is None

    asyncio.run(scenario())


def test_knowledge_injected_into_prompt_and_exemplar_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)
    monkeypatch.setattr(config, "ABAP_INTERPRET_RESULT", False)
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    (tmp_path / "kb").mkdir()
    store = make_store(tmp_path / "kb")
    program = "```abap\nREPORT zai_temp.\nSELECT COUNT(*) FROM zumowyl INTO lv_n WHERE status = '14'.\nWRITE lv_n.\n```"

    async def scenario():
        llm = FakeLLM([("Program:\n" + program, None)])
        mgr, orch, flow, session, q = setup(tmp_path, llm, store)
        orch.start_turn(session, "Ile jest aktywnych umów leasingowych?")
        await session.turn_task
        system = llm.seen[0][0]["content"]
        assert "Knowledge about this system" in system and "ZUMOWYL" in system
        assert "STATUS between 14 and 19" in system
        assert session.injected_notes and "tables/ZUMOWYL" in session.injected_notes
        run_id = [d for e, d in drain(q) if e == "code_ready"][0]["run_id"]
        assert flow.confirm(session, run_id)
        await session.turn_task
        notices = [d["text"] for e, d in drain(q) if e == "notice"]
        assert any(t.startswith("Kept this program as exemplars/") for t in notices)
        ex = [n for n in store.notes() if n.type == "exemplar"]
        assert len(ex) == 1 and ex[0].question == "Ile jest aktywnych umów leasingowych?"
        assert ex[0].tables == ["ZUMOWYL"]
        # the injected notes were credited with the success
        assert store.get("tables/ZUMOWYL").successes == 1

    asyncio.run(scenario())
