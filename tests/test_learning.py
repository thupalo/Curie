"""Learning stage two: insights, the report catalogue, feedback, truncation retry."""
import asyncio

from curie import config
from curie.memory.store import (KnowledgeStore, slot_values, template_signature,
                                template_slots)
from curie.sessions.model import State
from tests.test_knowledge import make_store, setup
from tests.test_orchestrator import FakeExecutor, FakeLLM, drain

PROGRAM_2004 = ("REPORT zai_x.\nDATA lv_n TYPE i.\nSELECT COUNT(*) FROM zumowyl INTO lv_n\n"
                "  WHERE rokum = '2004'.\nWRITE: / 'Umowy 2004:', lv_n.")

INSIGHT_JSON = ('{"insight": "On this system declare internal tables with OCCURS 0, '
                'TYPE STANDARD TABLE OF is rejected.", "keywords": ["OCCURS", "internal table"]}')


class LearningLLM(FakeLLM):
    """FakeLLM whose complete() also answers the insight prompt and counts audits."""

    def __init__(self, answers, insight=INSIGHT_JSON, finishes=None):
        super().__init__(answers)
        self.insight = insight
        self.finishes = list(finishes or [])
        self.audits = 0
        self.insight_calls = 0

    async def stream(self, messages, tools=None, **kw):
        self.seen.append(messages)
        text, calls = self.answers.pop(0)
        for piece in text.split(" "):
            yield ("delta", piece + " ")
            await asyncio.sleep(0)
        if calls:
            yield ("tool_calls", calls)
        finish = self.finishes.pop(0) if self.finishes else "stop"
        yield ("done", {"content": text, "finish_reason": finish})

    async def complete(self, messages, **kw):
        if messages[0]["content"].startswith("You distil pitfalls"):
            self.insight_calls += 1
            return self.insight
        self.audits += 1
        return self.audit


# -- store ---------------------------------------------------------------------- #

def test_insight_notes_retrieval_render_and_governance(tmp_path):
    store = make_store(tmp_path)
    note = store.add_insight("Use OCCURS 0 for internal tables; TYPE STANDARD TABLE OF fails here.",
                             source="curie:syntax", tables=["ZUMOWYL"], keywords=["OCCURS"])
    assert note.type == "insight" and note.status == "proposed" and note.id.startswith("insights/")
    # the same rule again only counts a use
    assert store.add_insight("Use OCCURS 0 for internal tables, TYPE STANDARD TABLE OF fails here",
                             source="curie:syntax") is None
    assert store.get(note.id).uses == 1
    general = store.add_insight("Keep every SELECT bounded with UP TO n ROWS or a WHERE clause.",
                                source="user:THUPALO", confirmed=True)
    assert general.status == "confirmed" and general.tables == []
    notes, _ = store.retrieve("ile umów w ZUMOWYL")
    ids = [n.id for n in notes]
    assert note.id in ids and general.id in ids       # general rules are always candidates
    text = store.render(notes, [])
    assert "Pitfalls and rules learned" in text and "(unconfirmed)" in text
    assert "UP TO n ROWS" in text
    # two successes promote a proposed insight, two failures retire it
    store.record([note.id], success=True)
    store.record([note.id], success=True)
    assert store.get(note.id).status == "confirmed"
    store.record([general.id], success=False)
    store.record([general.id], success=False)
    assert store.get(general.id).status == "retired"
    # a retired rule is not learned again
    assert store.add_insight("Keep every SELECT bounded with UP TO n ROWS or a WHERE clause.",
                             source="curie:rules") is None


def test_template_slots_and_catalogue_replay(tmp_path):
    store = make_store(tmp_path)
    q = "Ile umów zawarto w 2004 roku?"
    assert slot_values(q) == ["2004"] and template_slots(q, PROGRAM_2004) == ["2004"]
    assert template_signature(q) == template_signature("Ile umów zawarto w 2007 roku?")
    assert template_signature(q) != template_signature("Ile umów zawarto?")
    note = store.add_exemplar(q, PROGRAM_2004, "THUPALO", "AB12CD34",
                              audit={"allowed": True, "risk": "low", "summary": "read-only"})
    assert note.slots == ["2004"] and note.audit["allowed"] is True and note.catalogue is False
    assert store.find_template("Ile umów zawarto w 2007 roku?") is None   # not catalogued yet
    assert store.mark_catalogue(note.id).catalogue is True
    assert [n.id for n in store.catalogue()] == [note.id]
    found = store.find_template("Ile umów zawarto w 2007 roku?")
    assert found is not None and found[1] == {"2004": "2007"}
    program = store.instantiate(found[0], found[1])
    assert "WHERE rokum = '2007'" in program and "'Umowy 2007:'" in program
    # same question, same values: an empty mapping
    assert store.find_template("ile umów zawarto w 2004 roku")[1] == {}
    # a value the program does not carry is not a template match
    assert store.find_template("Ile umów zawarto w 2004 roku dla 15 klientów?") is None
    # retiring removes it from the catalogue; /forget on a template
    store.retire(note.id)
    assert store.catalogue() == [] and store.find_template("Ile umów zawarto w 2007 roku?") is None
    assert store.mark_catalogue("nope") is None


def test_co_read_from_exemplars(tmp_path):
    store = make_store(tmp_path)
    p = "REPORT z.\nSELECT * FROM zumowyl INTO wa WHERE x = 1.\nSELECT SINGLE * FROM zklient INTO k."
    store.add_exemplar("pytanie pierwsze o klienta", p, "A", "00000001")
    store.add_exemplar("pytanie drugie zupełnie inne", p, "A", "00000002")
    store.add_exemplar("trzecie pytanie", "REPORT z.\nSELECT * FROM zumowyl INTO wa WHERE x = 2.", "A", "00000003")
    lines = store.co_read(["ZUMOWYL"])
    assert lines == ["Confirmed programs that read ZUMOWYL also read ZKLIENT (2 of 3)."]
    assert store.co_read(["MARA"]) == []


# -- flow ----------------------------------------------------------------------- #

def test_replay_from_catalogue_skips_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_INTERPRET_RESULT", False)
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    (tmp_path / "kb").mkdir()
    store = make_store(tmp_path / "kb")
    note = store.add_exemplar("Ile umów zawarto w 2004 roku?", PROGRAM_2004, "THUPALO", "AB12CD34",
                              audit={"allowed": True, "risk": "low", "summary": "read-only"})
    store.mark_catalogue(note.id)

    async def scenario():
        llm = LearningLLM([])
        ex = FakeExecutor(lines=["Umowy 2007: 12"])
        mgr, orch, flow, session, q = setup(tmp_path, llm, store, ex)
        assert orch.start_turn(session, "Ile umów zawarto w 2007 roku?")
        await session.turn_task
        events = drain(q)
        assert llm.seen == []                         # no generation call
        kinds = [e for e, d in events if e != "state"]
        assert kinds[0] == "user_message"             # shown before the catalogue was consulted
        notices = [d["text"] for e, d in events if e == "notice"]
        assert notices and notices[0].startswith("Report catalogue: this question matches " + note.id)
        card = [d for e, d in events if e == "code_ready"][0]
        assert card["origin"] == {"catalogue": note.id, "values": {"2004": "2007"}}
        assert "rokum = '2007'" in card["source"] and card["can_run"]
        assert llm.audits == 1                        # values changed: reviewed again
        assert session.state is State.CONFIRM
        assert flow.confirm(session, card["run_id"])
        await session.turn_task
        assert ex.executed == [card["progname"]]
        assert store.get(note.id).successes == 1      # the template was credited
        assert [n for n in store.notes() if n.type == "exemplar"] == [store.get(note.id)]

        # the same question with the same values reuses the stored verdict
        assert orch.start_turn(session, "ile umów zawarto w 2004 roku")
        await session.turn_task
        card = [d for e, d in drain(q) if e == "code_ready"][0]
        assert llm.audits == 1 and "catalogue verdict" in card["audit"]["summary"]
        flow.discard(session, card["run_id"])

        # a different question shape goes to the model as before
        llm.answers = [("Nie wiem.", None)]
        assert orch.start_turn(session, "Kto jest klientem umowy 100/2005?")
        await session.turn_task
        assert len(llm.seen) == 1

    asyncio.run(scenario())


def test_insight_learned_after_a_fix_round(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)
    monkeypatch.setattr(config, "ABAP_INTERPRET_RESULT", False)
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    (tmp_path / "kb").mkdir()
    store = make_store(tmp_path / "kb")
    bad = "```abap\nREPORT zai_temp.\nDATA lt TYPE STANDARD TABLE OF zumowyl.\nSELECT * FROM zumowyl INTO TABLE lt UP TO 5 ROWS.\n```"
    good = "```abap\nREPORT zai_temp.\nDATA lt LIKE zumowyl OCCURS 0 WITH HEADER LINE.\nSELECT * FROM zumowyl INTO TABLE lt UP TO 5 ROWS.\n```"

    async def scenario():
        llm = LearningLLM([("Try:\n" + bad, None), ("Fixed:\n" + good, None)])
        ex = FakeExecutor(syntax_ok=[False, True])
        mgr, orch, flow, session, q = setup(tmp_path, llm, store, ex)
        orch.start_turn(session, "Pokaż 5 umów")
        await session.turn_task
        cards = [d for e, d in drain(q) if e == "code_ready"]
        assert len(cards) == 2 and session.fixes and session.fixes[0]["kind"] == "syntax"
        run = session.runs[cards[1]["run_id"]]
        assert len(run.fixes) == 1
        assert flow.confirm(session, run.run_id)
        await session.turn_task
        for _ in range(5):                            # the insight task runs after the result
            await asyncio.sleep(0)
        assert llm.insight_calls == 1
        insights = [n for n in store.notes() if n.type == "insight"]
        assert len(insights) == 1 and insights[0].source == "curie:syntax"
        assert insights[0].tables == ["ZUMOWYL"] and "LV_X" in insights[0].keywords
        assert insights[0].body.startswith("On this system declare internal tables")
        notices = [d["text"] for e, d in drain(q) if e == "notice"]
        assert any(t.startswith("Learned a pitfall (insights/") for t in notices)
        # NONE means nothing is learned
        llm.insight = '{"insight": "NONE", "keywords": []}'
        run.fixes = [{"kind": "rules", "message": "x", "word": "", "line": 0, "source": ""}]
        await flow._learn_insights(session, run)
        assert len([n for n in store.notes() if n.type == "insight"]) == 1

    asyncio.run(scenario())


def test_feedback_credits_notes_and_fills_the_catalogue(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)
    monkeypatch.setattr(config, "ABAP_INTERPRET_RESULT", False)
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    (tmp_path / "kb").mkdir()
    store = make_store(tmp_path / "kb")
    program = "```abap\n" + PROGRAM_2004 + "\n```"

    async def scenario():
        llm = LearningLLM([("Program:\n" + program, None)])
        mgr, orch, flow, session, q = setup(tmp_path, llm, store)
        orch.start_turn(session, "Ile umów zawarto w 2004 roku w ZUMOWYL?")
        await session.turn_task
        events = drain(q)
        answer_id = [d for e, d in events if e == "assistant_done"][0]["id"]
        card = [d for e, d in events if e == "code_ready"][0]
        assert card["origin"] is None
        # thumbs down on the answer debits what was injected
        assert orch.feedback(session, "answer", answer_id, "down").startswith("Noted: ")
        assert store.get("tables/ZUMOWYL").failures == 1
        assert orch.feedback(session, "answer", answer_id, "down") == "Already noted."
        assert session.messages[-1]["content"].startswith("[The user marked the last answer as wrong.]")
        # run it, then mark the result correct: it becomes a catalogue entry
        assert flow.confirm(session, card["run_id"])
        await session.turn_task
        run = session.runs[card["run_id"]]
        assert run.exemplar_id.startswith("exemplars/")
        text = orch.feedback(session, "run", card["run_id"], "up")
        assert text.startswith("Marked correct: " + run.exemplar_id) and "2004" in text
        assert store.get(run.exemplar_id).catalogue is True
        assert store.find_template("Ile umów zawarto w 2005 roku w ZUMOWYL?")[1] == {"2004": "2005"}
        assert orch.command(session, "/catalogue") == "catalogue"
        listing = [d["text"] for e, d in drain(q) if e == "notice"][-1]
        assert "1 catalogued report" in listing and run.exemplar_id in listing
        assert orch.command(session, f"/catalogue drop {run.exemplar_id}") == "catalogue"
        assert store.get(run.exemplar_id).catalogue is False
        assert orch.command(session, f"/catalogue add {run.exemplar_id}") == "catalogue"
        assert store.get(run.exemplar_id).catalogue is True

    asyncio.run(scenario())


def test_feedback_wrong_on_a_run_retires_the_exemplar(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)
    monkeypatch.setattr(config, "ABAP_INTERPRET_RESULT", False)
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    (tmp_path / "kb").mkdir()
    store = make_store(tmp_path / "kb")

    async def scenario():
        llm = LearningLLM([("Program:\n```abap\n" + PROGRAM_2004 + "\n```", None)])
        mgr, orch, flow, session, q = setup(tmp_path, llm, store)
        orch.start_turn(session, "Ile umów zawarto w 2004 roku?")
        await session.turn_task
        card = [d for e, d in drain(q) if e == "code_ready"][0]
        flow.confirm(session, card["run_id"])
        await session.turn_task
        run = session.runs[card["run_id"]]
        assert orch.feedback(session, "run", run.run_id, "down").startswith("Marked wrong: ")
        assert store.get(run.exemplar_id).status == "retired"

    asyncio.run(scenario())


def test_truncated_answer_is_asked_again_with_a_bigger_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_OUTPUT_TOKENS", 100)

    async def scenario():
        llm = LearningLLM([("The list of plants is", None), ("The list of plants is long.", None)],
                          finishes=["length", "stop"])
        mgr, orch, flow, session, q = setup(tmp_path, llm, None)
        orch.start_turn(session, "list the plants")
        await session.turn_task
        events = drain(q)
        dones = [d for e, d in events if e == "assistant_done"]
        assert dones[0]["truncated"] is True and dones[1]["text"] == "The list of plants is long."
        assert any("cut at 100 tokens" in d["text"] for e, d in events if e == "notice")
        assert session.messages[-1]["content"] == "The list of plants is long."
        assert len(llm.seen) == 2

    asyncio.run(scenario())


# -- threads: follow-ups, question rewrite, preference hints ----------------------- #

def test_followup_detection():
    from curie.memory.threads import compose_question, is_followup
    yes = ["Wygeneruj jeszcze raz poprawiając problem",
           "Pomiń kolumnę TOW* skup się nad zasadniczym problemem",
           "Wygeneruj histogram rozkładu dni opóźnienia dla tych danych",
           "Dodaj kolumnę z numerem rejestracyjnym", "Posortuj po dacie",
           "Pokaż to samo dla umowy 200/2005", "Do the same for 2005", "Fix the date logic",
           "Show only the first 10 rows of the previous list"]
    no = ["Ile umów zawarto w 2004 roku?", "Znajdź umowy w których przedmiotem był Quad?",
          "Jakie polisy zostały wystawione dla pojazdu CB83602?",
          "Dla umów zawartych w maju 2005r pokaż statystykę dnia miesiąca płatności rat",
          "A teraz pokaż historię stanów dla umowy 200/2005",
          "Podaj tylko liczbę aktywnych umów", "How many plants are there?"]
    assert all(is_followup(t) for t in yes), [t for t in yes if not is_followup(t)]
    assert not any(is_followup(t) for t in no), [t for t in no if is_followup(t)]
    assert not is_followup("Wygeneruj jeszcze raz", has_thread=False)
    assert compose_question("Ile umów?", ["bez kolumny TOW"]) == "Ile umów (bez kolumny TOW)"


THREAD_JSON = ('{"question": "Czy umowa 100/2005 miała okres bez aktywnej polisy, gdy była aktywna?", '
               '"hint": "Pomijaj kolumny TOW* w listach umów"}')


class ThreadLLM(LearningLLM):
    def __init__(self, answers, thread_json=THREAD_JSON):
        super().__init__(answers)
        self.thread_json = thread_json
        self.thread_calls = 0

    async def complete(self, messages, **kw):
        if messages[0]["content"].startswith("A user asked a business question"):
            self.thread_calls += 1
            return self.thread_json
        return await super().complete(messages, **kw)


def test_followups_capture_one_exemplar_under_the_business_question(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)
    monkeypatch.setattr(config, "ABAP_INTERPRET_RESULT", False)
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    (tmp_path / "kb").mkdir()
    store = make_store(tmp_path / "kb")
    p1 = "```abap\nREPORT zai_temp.\nSELECT * FROM zubezp INTO wa WHERE nrum = '100' AND tow = 'X'.\nWRITE wa.\n```"
    p2 = "```abap\nREPORT zai_temp.\nSELECT * FROM zubezp INTO wa WHERE nrum = '100'.\nWRITE wa-nrum.\n```"
    question = "Czy istnieje przedział czasu kiedy umowa 100/2005 nie posiadała aktywnej polisy?"

    async def run_and_confirm(orch, flow, session, q, prompt):
        orch.start_turn(session, prompt)
        await session.turn_task
        card = [d for e, d in drain(q) if e == "code_ready"][0]
        assert flow.confirm(session, card["run_id"])
        await session.turn_task
        for _ in range(5):
            await asyncio.sleep(0)
        return session.runs[card["run_id"]]

    async def scenario():
        llm = ThreadLLM([("Program:\n" + p1, None), ("Again:\n" + p2, None)])
        mgr, orch, flow, session, q = setup(tmp_path, llm, store)
        run1 = await run_and_confirm(orch, flow, session, q, question)
        assert session.thread == [question] and llm.thread_calls == 0
        first = store.get(run1.exemplar_id)
        assert first.question == question and first.refinements == []

        run2 = await run_and_confirm(orch, flow, session, q, "Pomiń kolumnę TOW* skup się nad zasadniczym problemem")
        assert session.thread[1].startswith("Pomiń") and llm.thread_calls == 1
        # the thread's exemplar was updated, not duplicated
        assert run2.exemplar_id == run1.exemplar_id
        assert [n.id for n in store.notes() if n.type == "exemplar"] == [run1.exemplar_id]
        note = store.get(run1.exemplar_id)
        assert note.question.startswith("Czy umowa 100/2005 miała okres bez aktywnej polisy")
        assert note.refinements == ["Pomiń kolumnę TOW* skup się nad zasadniczym problemem"]
        assert "tow = 'X'" not in note.program and "Refinements the user asked for" in note.body
        # the preference became a hint with the run's tables
        hints = [n for n in store.notes() if n.type == "hint"]
        assert len(hints) == 1 and hints[0].body == "Pomijaj kolumny TOW* w listach umów"
        assert hints[0].tables == ["ZUBEZP"] and hints[0].source == "user:HUPALO"
        notices = [d["text"] for e, d in drain(q) if e == "notice"]
        assert any(t.startswith("Updated exemplars/") for t in notices)
        assert any(t.startswith("Remembered your preference as hints/") for t in notices)

        # a new question starts a new thread
        llm.answers = [("Nie wiem.", None)]
        orch.start_turn(session, "Ile umów zawarto w 2004 roku?")
        await session.turn_task
        assert session.thread == ["Ile umów zawarto w 2004 roku?"] and session.thread_exemplar_id == ""

    asyncio.run(scenario())


def test_hint_alias_and_correct_before_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)
    monkeypatch.setattr(config, "ABAP_INTERPRET_RESULT", False)
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    (tmp_path / "kb").mkdir()
    store = make_store(tmp_path / "kb")

    async def scenario():
        llm = ThreadLLM([("Program:\n```abap\n" + PROGRAM_2004 + "\n```", None),
                         ("Again:\n```abap\n" + PROGRAM_2004.replace("2004", "2004 ") + "\n```", None)],
                        thread_json='{"question": "Ile umów zawarto w 2004 roku?", "hint": null}')
        mgr, orch, flow, session, q = setup(tmp_path, llm, store)
        assert orch.command(session, "/hint Rok umowy to ROKUM w ZUMOWYL") == "remember"
        hints = [n for n in store.notes() if n.type == "hint"]
        assert len(hints) == 1 and hints[0].tables == ["ZUMOWYL"]
        assert orch.command(session, "/hint") == "remember"          # usage notice
        # first run: captured synchronously
        orch.start_turn(session, "Ile umów zawarto w 2004 roku?")
        await session.turn_task
        card = [d for e, d in drain(q) if e == "code_ready"][0]
        flow.confirm(session, card["run_id"])
        await session.turn_task
        run1 = session.runs[card["run_id"]]
        assert run1.exemplar_id
        # follow-up run: capture is a task; Correct pressed before it lands
        orch.start_turn(session, "Wygeneruj jeszcze raz poprawiając problem")
        await session.turn_task
        card = [d for e, d in drain(q) if e == "code_ready"][0]
        flow.confirm(session, card["run_id"])
        await session.turn_task
        run2 = session.runs[card["run_id"]]
        # Correct may arrive before or after the capture task lands; both
        # end with the thread's exemplar in the catalogue.
        text = orch.feedback(session, "run", run2.run_id, "up")
        assert text.startswith("Marked correct:")
        for _ in range(5):
            await asyncio.sleep(0)
        assert run2.exemplar_id == run1.exemplar_id
        assert store.get(run1.exemplar_id).catalogue is True
        assert len([n for n in store.notes() if n.type == "hint"]) == 1   # hint null: none added

    asyncio.run(scenario())


# -- K6: audit memo, one outcome per thread ------------------------------------------ #

def test_audit_memo_skips_the_review_for_an_equal_program_shape(tmp_path, monkeypatch):
    from curie.memory.audit_memo import AuditMemo
    monkeypatch.setattr(config, "ABAP_INTERPRET_RESULT", False)
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    (tmp_path / "kb").mkdir()
    store = make_store(tmp_path / "kb")
    p2004 = "```abap\n" + PROGRAM_2004 + "\n```"
    p2005 = "```abap\n" + PROGRAM_2004.replace("2004", "2005") + "\n```"

    async def scenario():
        llm = LearningLLM([("Program:\n" + p2004, None), ("Program:\n" + p2005, None)])
        mgr, orch, flow, session, q = setup(tmp_path, llm, store)
        flow.audit_memo = AuditMemo(enabled=True)
        orch.start_turn(session, "Ile umów zawarto w 2004 roku w ZUMOWYL?")
        await session.turn_task
        card = [d for e, d in drain(q) if e == "code_ready"][0]
        assert llm.audits == 1 and card["can_run"] and not card["audit"].get("memo")
        assert len(flow.audit_memo) == 1
        flow.discard(session, card["run_id"])
        orch.start_turn(session, "Ile umów zawarto w 2005 roku w ZUMOWYL?")
        await session.turn_task
        card = [d for e, d in drain(q) if e == "code_ready"][0]
        assert llm.audits == 1                        # same shape: the verdict was reused
        assert card["audit"]["memo"] and "review not repeated" in card["audit"]["summary"] and card["can_run"]

    asyncio.run(scenario())


def test_one_outcome_per_thread_and_only_used_notes_are_credited(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)
    monkeypatch.setattr(config, "ABAP_INTERPRET_RESULT", False)
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    (tmp_path / "kb").mkdir()
    store = make_store(tmp_path / "kb")
    other = store.add_insight("Keep every SELECT bounded with UP TO n ROWS or a WHERE clause.",
                              source="user:THUPALO", confirmed=True)
    program = "```abap\n" + PROGRAM_2004 + "\n```"

    async def scenario():
        llm = LearningLLM([("Program:\n" + program, None), ("Second answer", None)])
        mgr, orch, flow, session, q = setup(tmp_path, llm, store)
        orch.start_turn(session, "Ile umów zawarto w 2004 roku w ZUMOWYL?")
        await session.turn_task
        first = [d for e, d in drain(q) if e == "assistant_done"][0]["id"]
        assert "tables/ZUMOWYL" in session.thread_notes and other.id in session.thread_notes
        text = orch.feedback(session, "answer", first, "up")
        assert text.startswith("Noted: ") and "credited, 1 shown but not used" in text
        assert store.get("tables/ZUMOWYL").successes == 1           # ZUMOWYL is read by the program
        assert store.get(other.id).successes == 0 and store.get(other.id).uses == 1   # shown, not used
        # A follow-up in the same thread: the outcome was already counted.
        orch.start_turn(session, "Pomiń kolumnę TOW")
        await session.turn_task
        second = [d for e, d in drain(q) if e == "assistant_done"][0]["id"]
        assert session.thread_id in session.thread_outcome
        assert orch.feedback(session, "answer", second, "down") == \
            "Noted; this question's outcome was already counted (one per question)."
        assert store.get("tables/ZUMOWYL").failures == 0

    asyncio.run(scenario())


# -- who may teach: KNOWLEDGE_FEEDBACK all | owners | off ---------------------------- #

def test_feedback_counts_only_for_owners_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ABAP_AUDIT", False)
    monkeypatch.setattr(config, "ABAP_INTERPRET_RESULT", False)
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    monkeypatch.setattr(config, "KNOWLEDGE_FEEDBACK", "owners")
    monkeypatch.setattr(config, "KNOWLEDGE_OWNERS", ["KNOW*"])
    (tmp_path / "kb").mkdir()
    store = make_store(tmp_path / "kb")
    program = "```abap\n" + PROGRAM_2004 + "\n```"

    async def scenario():
        llm = LearningLLM([("Program:\n" + program, None)])
        mgr, orch, flow, session, q = setup(tmp_path, llm, store)     # the session's user is HUPALO
        orch.start_turn(session, "Ile umów zawarto w 2004 roku w ZUMOWYL?")
        await session.turn_task
        events = drain(q)
        answer_id = [d for e, d in events if e == "assistant_done"][0]["id"]
        card = [d for e, d in events if e == "code_ready"][0]
        # Not an owner: the vote is acknowledged and logged, nothing changes.
        assert orch.feedback(session, "answer", answer_id, "down") == "Noted, thanks."
        assert store.get("tables/ZUMOWYL").failures == 0 and session.thread_outcome == {}
        assert session.messages[-1]["content"].startswith("[The user marked the last answer as wrong.]")
        assert flow.confirm(session, card["run_id"])
        await session.turn_task
        run = session.runs[card["run_id"]]
        assert orch.feedback(session, "run", card["run_id"], "up") == "Noted, thanks."
        assert store.get(run.exemplar_id).catalogue is False           # no admission by a non-owner
        # An owner's vote counts as before.
        session.sap = session.sap.__class__(**{**session.sap.__dict__, "user": "KNOWLEDGE1"})
        assert orch.feedback(session, "run", card["run_id"] + "x", "up") == "Noted, thanks."   # unknown run
        session.feedback.clear()
        text = orch.feedback(session, "run", card["run_id"], "up")
        assert text.startswith("Marked correct: ") and store.get(run.exemplar_id).catalogue is True
        # Off: the endpoint's text, nothing else.
        monkeypatch.setattr(config, "KNOWLEDGE_FEEDBACK", "off")
        assert orch.feedback(session, "answer", answer_id, "up") == "Feedback is switched off on this installation."

    asyncio.run(scenario())


def test_owner_patterns():
    monkeypatch_owner = config.KNOWLEDGE_OWNERS
    try:
        config.KNOWLEDGE_OWNERS = ["THUPALO", "KNOW*"]
        assert config.is_owner("thupalo") and config.is_owner("KNOWLEDGE2") and not config.is_owner("USER1")
        config.KNOWLEDGE_FEEDBACK = "owners"
        assert config.feedback_counts("THUPALO") and not config.feedback_counts("USER1")
        config.KNOWLEDGE_FEEDBACK = "all"
        assert config.feedback_counts("USER1")
        config.KNOWLEDGE_FEEDBACK = "off"
        assert not config.feedback_counts("THUPALO")
    finally:
        config.KNOWLEDGE_OWNERS = monkeypatch_owner
        config.KNOWLEDGE_FEEDBACK = "all"
