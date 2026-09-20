"""The evaluation loop: the question bank, the measures, the report."""
import asyncio

from curie.archive import Archive
from curie.memory import evaluate as ev
from curie.memory.evaluate import Case, CaseResult
from tests.test_archive import new_format_session, old_format_session, write_log


def test_question_bank_from_the_archive(tmp_path):
    root = tmp_path / "logs"
    write_log(root / "2026-09-11" / "AAAA1111.jsonl", new_format_session())
    write_log(root / "2026-09-09" / "BBBB2222.jsonl", old_format_session())
    write_log(root / "eval" / "2026-09-11" / "EEEE0000.jsonl", new_format_session())   # never an episode
    archive = Archive(tmp_path / "archive" / "curie.db")
    archive.import_dir(root)
    assert archive.stats()["sessions"] == 2
    bank = ev.question_bank(archive)
    assert [c.question for c in bank] == ["Ile umów zawarto w 2004 roku?"]
    assert bank[0].tables == ["ZUMOWYL"] and "SELECT COUNT(*)" in bank[0].program
    assert bank[0].refinements == ["Pomiń kolumnę TOW"]
    assert ev.question_bank(archive, ids=["nope"]) == []
    archive.close()


def general_first_session(t0=1789100000.0):
    """A thread whose opening question was answered without SAP ("how do I
    see a balance in SAP GUI") and whose follow-up led to a program."""
    records = new_format_session(t0)
    first_tools = {"tool_call", "tool_result", "tool"}
    out = []
    for r in records:
        if r["kind"] in first_tools:
            continue                                  # the first turn asked nothing
        if r.get("run_id") == "R1":
            r = dict(r, run_id="R9")                  # run ids are unique across the archive
        if r["kind"] == "user_message" and r["id"] == "u1":
            r = dict(r, text="Pokaż mi jak wyświetlić saldo klienta w FI za pomocą SAP GUI")
        if r["kind"] == "llm_user" and not r.get("auto"):
            r = dict(r, text="Pokaż mi jak wyświetlić saldo klienta w FI za pomocą SAP GUI")
        if r["kind"] == "llm_done" and r["id"] == "a1":
            r = dict(r, finish="stop", tool_calls=0, chars=900)
        if r["kind"] == "assistant_done" and r["id"] == "a1":
            r = dict(r, text="Use FD10 ...")
        out.append(r)
        if r["kind"] == "assistant_done" and r["id"] == "a1":
            # The follow-up that turned the thread to the system's data,
            # before the second model turn wrote the program.
            out.append({"ts": t0 + 5.5, "kind": "user_message", "id": "u1b", "thread": "u1", "followup": True,
                        "text": "Tak. Napisz program, który policzy umowy z 2004"})
            out.append({"ts": t0 + 5.5, "kind": "llm_user", "auto": False, "thread": "u1",
                        "text": "Tak. Napisz program, który policzy umowy z 2004"})
    return out


def test_bank_excludes_threads_that_opened_without_asking_sap(tmp_path):
    root = tmp_path / "logs"
    write_log(root / "2026-09-11" / "AAAA1111.jsonl", new_format_session())
    write_log(root / "2026-09-12" / "CCCC3333.jsonl", general_first_session())
    archive = Archive(tmp_path / "archive" / "curie.db")
    archive.import_dir(root)
    assert ev.opening_without_sap(archive, "CCCC3333-001") is True
    assert ev.opening_without_sap(archive, "AAAA1111-001") is False   # its first turn read a structure
    excluded: list = []
    bank = ev.question_bank(archive, excluded=excluded)
    assert [c.id for c in bank] == ["AAAA1111-001"]
    assert excluded == [{"id": "CCCC3333-001", "reason": "opening question answered without asking SAP"}]
    everything = ev.question_bank(archive, include_general=True)
    assert [(c.id, c.general) for c in everything] == [("CCCC3333-001", True), ("AAAA1111-001", False)]
    report = ev.report_markdown({"started": 1789000000.0, "seconds": 1, "model": "m", "cases": [],
                                 "summary": ev.summarize([]), "excluded": excluded})
    assert "1 case(s) excluded" in report and "- CCCC3333-001: opening question answered without asking SAP" in report
    archive.close()


def test_bank_excludes_programs_whose_values_are_not_in_the_question():
    program = ("REPORT z.\nDATA lv_kunnr LIKE kna1-kunnr.\nlv_kunnr = '0000125812'.\n"
               "SELECT SINGLE akont FROM knb1 INTO lv_akont WHERE kunnr = lv_kunnr AND bukrs = 'CARC'.")
    assert ev.borrowed_values("Na podstawie wiedzy ogólnej podaj sposób wyznaczenia konta zbiorczego", program) \
        == ["0000125812"]
    assert ev.borrowed_values("Podaj konto zbiorcze odbiorcy 125812 w jednostce CARC", program) == []
    # Years and agreement numbers named in the question are not borrowed; UP TO and short codes never count.
    year = ("REPORT z.\nSELECT COUNT(*) FROM zumowyl INTO lv WHERE rokum = '2004' AND nrum = '0000100' "
            "AND datakon <> '00000000' UP TO 10 ROWS.")
    assert ev.borrowed_values("Ile rat ma umowa 100/2004?", year) == []
    assert ev.borrowed_values("Ile umów zawarto?", year) == ["2004", "0000100"]
    case = ev.Case(id="X", question="Ile umów zawarto?", program=year, borrowed=["2004", "0000100"])
    assert case.excluded_reason == "accepted program uses values that are not in the question: 2004, 0000100"


def test_summary_and_report_with_previous(tmp_path):
    case = Case(id="AAAA1111-001", question="Ile umów zawarto w 2004 roku?",
                program="REPORT z.\nSELECT COUNT(*) FROM zumowyl INTO lv WHERE rokum = '2004'.", tables=["ZUMOWYL"])
    good = CaseResult(case=case, attempts=1, first_syntax_ok=True, runnable=True, audit="low",
                      shape_equal=True, tables_equal=True, model_calls=2, tool_calls=1, seconds=12.0,
                      context_chars=800)
    bad = CaseResult(case=Case(id="B", question="Saldo?", program="REPORT z.", tables=[]), attempts=3,
                     first_syntax_ok=False, runnable=False, audit="denied", tool_errors=2, seconds=90.0)
    none = CaseResult(case=Case(id="N", question="Napisz program liczący umowy", program="REPORT z.", tables=[]),
                      attempts=0, seconds=8.0)
    summary = ev.summarize([good, bad, none])
    assert summary["cases"] == 3 and summary["runnable"] == 1 and summary["first_syntax_ok"] == 1
    assert summary["program_expected"] == 1 and summary["program_missing"] == 1
    assert "| program asked for but none written | 1 / 1 | - |" in ev.report_markdown(
        {"started": 1789000000.0, "seconds": 1, "model": "m", "cases": [none.row()], "summary": summary})
    summary = ev.summarize([good, bad])
    assert summary["cases"] == 2 and summary["runnable"] == 1 and summary["first_syntax_ok"] == 1
    assert summary["denied"] == 1 and summary["attempts_avg"] == 2.0 and summary["tool_errors"] == 2
    result = {"started": 1789000000.0, "seconds": 102.0, "model": "qwen",
              "cases": [good.row(), bad.row()], "summary": summary}
    previous = {"summary": {**summary, "runnable": 0, "cases": 2, "syntax_checked": 2}}
    text = ev.report_markdown(result, previous)
    assert "| runnable after the checks | 1 / 2 | 0 / 2 |" in text
    assert "| AAAA1111-001 |" in text and "| denied |" in text
    path = ev.write_report(text, tmp_path)
    assert path.name == "EVAL.md" and path.read_text(encoding="utf-8").startswith("# Evaluation")
    archive = Archive(tmp_path / "archive" / "curie.db")
    archive.save_eval(result)
    last = archive.last_eval()
    assert last["summary"]["runnable"] == 1 and len(last["cases"]) == 2
    archive.close()


def test_evaluator_measures_a_session(monkeypatch):
    """A fake manager/orchestrator: the evaluator reads runs and the transcript."""
    import time
    from types import SimpleNamespace

    class Run:
        def __init__(self, source, syntax, audit, can_run, origin=None, created=0.0):
            self.source, self.syntax, self.audit, self.can_run = source, syntax, audit, can_run
            self.origin, self.created, self.status = origin, created, "ready"

    session = SimpleNamespace(runs={}, transcript=[], messages=[], turn_task=None)

    class Manager:
        def open_dev(self, prompt, user=""):
            return session

        def close(self, s, reason):
            s.closed = reason

    class Orchestrator:
        knowledge = None
        prompts = []

        def start_turn(self, s, text):
            self.prompts.append(text)

            async def turn():
                if text != "Bez komentarzy":
                    return
                s.transcript += [("assistant_start", {}), ("tool_call", {}), ("tool_result", {"ok": False}),
                                 ("assistant_start", {})]
                s.runs["R1"] = Run("REPORT z.\nSELECT COUNT(*) FROM zumowyl INTO lv WHERE rokum = '2003'.",
                                   {"ok": False, "message": "x"}, None, False, created=1.0)
                s.runs["R2"] = Run("REPORT z.\nSELECT COUNT(*) FROM zumowyl INTO lv WHERE rokum = '2003'.",
                                   {"ok": True}, {"allowed": True, "risk": "low"}, True, created=2.0)
            s.turn_task = asyncio.ensure_future(turn())
            return True

        def stop(self, s):
            return True

    case = Case(id="C1", question="Ile umów zawarto w 2003?",
                program="REPORT z.\nSELECT COUNT(*) FROM zumowyl INTO lv WHERE rokum = '2004'.", tables=["ZUMOWYL"],
                refinements=["Bez komentarzy"])
    orchestrator = Orchestrator()
    evaluator = ev.Evaluator(Manager(), orchestrator)
    result = asyncio.run(evaluator.run([case]))
    assert orchestrator.prompts == ["Ile umów zawarto w 2003?", "Bez komentarzy"]   # the thread, turn by turn
    row = result["cases"][0]
    assert row["attempts"] == 2 and row["first_syntax_ok"] is False and row["runnable"] is True
    assert row["audit"] == "low" and row["shape_equal"] and row["tables_equal"]
    assert row["model_calls"] == 2 and row["tool_calls"] == 1 and row["tool_errors"] == 1
    assert session.closed == "eval" and all(r.status == "discarded" for r in session.runs.values())
    assert result["summary"]["runnable"] == 1


def test_case_timeout_is_one_budget_for_all_turns():
    """Two turns of 0.3 s each against a 0.4 s budget: the second turn runs
    out, the case reports the timeout instead of taking 0.6 s."""
    from types import SimpleNamespace

    session = SimpleNamespace(runs={}, transcript=[], messages=[], turn_task=None)

    class Manager:
        def open_dev(self, prompt, user=""):
            return session

        def close(self, s, reason):
            s.closed = reason

    class Orchestrator:
        knowledge = None
        stopped = 0

        def start_turn(self, s, text):
            async def turn():
                await asyncio.sleep(0.3)
            s.turn_task = asyncio.ensure_future(turn())
            return True

        def stop(self, s):
            self.stopped += 1
            s.turn_task.cancel()
            return True

    case = Case(id="T", question="Ile umów?", program="REPORT z.", refinements=["Bez komentarzy"])
    orchestrator = Orchestrator()
    result = asyncio.run(ev.Evaluator(Manager(), orchestrator).run([case], timeout=0.4))
    row = result["cases"][0]
    assert row["error"] == "timeout after 0s" and orchestrator.stopped == 1 and row["seconds"] < 0.6
