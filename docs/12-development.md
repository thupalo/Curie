# 12 Development

## Layout

```
sap_ai_chat/
  pyproject.toml           package "curie-sap", console script "curie", pinned dependencies
  requirements.txt         -e .[dev]
  src/curie/               the code, installed as package `curie`
    cli.py                 entry point (curie, python -m curie): --check, --selftest
    config.py  caches.py  sapnwrfc720.py
    rfc/        client.py
    handlers/   registry.py  connectivity.py  session.py  zbla_chat.py  example_z.py
    chat/       protocol.py  service.py  selftest.py   (the plugin, chapter 15)
    sessions/   model.py  manager.py
    sap_tools/  read_table.py  ddic.py  function_interface.py  domain_values.py
                abap_source.py  abap_exec.py
    memory/     store.py
    llm/        client.py  prompts.py  tools.py  orchestrator.py  audit.py  abap_flow.py
    web/        app.py  static/index.html  static/app.js  static/style.css
    tools/      phase0.py  devchat.py  rfc_dev.py  rfc_server.py  trace.py
  tests/                   pytest suite with fakes
  knowledge/               Markdown notes the model reads (chapter 14); git-ignored except README.md
  archive/                 the episode archive, SQLite (git-ignored)
  cache/                   persisted dictionary caches (git-ignored)
  abap/                    sources and notes for the SAP side
  docs/                    this documentation, build_html.py
  service/                 install-service.ps1, install-tasks.ps1; service/logs/ git-ignored
  logs/                    session audit trail (git-ignored)
  .env                     here or one level up
```

Code and data are separate: everything under `src/` is the package, everything
beside it belongs to the installation. The project directory (`CURIE_HOME`,
default the current directory) anchors `.env`, `knowledge/`, `logs/` and the
log file; the service installer sets it.
```

`python -m curie.tools.rfc_server` is the bare RFC server the project
started as; it still runs on its own and serves whatever is in `handlers/`.

## Tests

```powershell
.\venv\Scripts\python -m pytest -q tests
```

194 tests, about ten seconds, no SAP and no model needed. They cover field
planning against the 512-character row, OPTIONS packing, result parsing,
retries on `DATA_BUFFER_EXCEEDED`, the deny list, fence extraction, line
folding, codepage sanitising, the static rules and the function policy,
audit parsing, session handlers, replay, the heartbeat monitor, list
delivery, per-user limits, IP binding, the turn loop with tool calls, the
code path with confirmation and result feedback, syntax-error round trips,
the retry limit, Stop during streaming and during a run, history trimming,
the HTTP layer including `/reset`, the caches, the knowledge store with its commands and exemplar capture, the domain-values tool, the chat plugin (chunker, line assembler, service with a scripted model, handlers), and learning stage two (insights, the report catalogue with replay, feedback, the truncation retry). The SDK DLL is loaded at import
(`sapnwrfc720`), so the SDK must be installed even for tests.

Fakes live in `tests/test_orchestrator.py` (`FakeLLM`, `FakeToolBox`,
`FakeExecutor`) and `tests/test_read_table.py` (`FakeSap`, `FakeDdic`).
The `TestClient` delivers a streaming body only when the response ends, so
SSE tests close the session first.

## Live tools

- `python -m curie.tools.phase0 [--table T --where "..." --fields a,b
  --structure S --fm F] [--no-run] [--job]`: release, raw interfaces, a structure with texts, a
  function interface, a table read; then syntax check (good and bad), and
  the hello program in synchronous mode. `--job` schedules it as a job (the
  delivery then fails, which shows the `TBTCO` fallback).
- `python -m curie.tools.devchat [--run] [--url http://host:8765] "prompt"`: needs Curie with
  `CURIE_DEV=1`; prints every event; `--run` confirms the first runnable
  code card and waits for the list.
- `curie --selftest`: see 11.
- `curie --trace <session id prefix | file>`, `--trace --newest [eval|user]`,
  `--full`: the timeline of a session log, the first tool to reach for
  when a turn or an evaluation case went wrong (11). `tools/trace.py`
  `describe()` is where a new record kind gets its line.
- Reading SAP sources for diagnostics: `RPY_PROGRAM_READ` and
  `RPY_FUNCTIONMODULE_READ` are remote-enabled on 4.5B (see the snippets in
  the session history of this project, or write a five-line script with
  `SapClient.call_known`).

## Adding things

**A tool for the model**: implement it in `sap_tools/`, add its JSON schema
to `TOOL_SCHEMAS` in `llm/tools.py`, dispatch it in `ToolBox.dispatch()`,
describe it in `TOOL_GUIDE` in `llm/prompts.py`, and add a fake-based test.
Tools run in the client pool; keep them blocking and thread-safe.

**A function module served to SAP**: create it in SE37 (remote-enabled,
interface only), drop a module in `handlers/` with `@handler("NAME")`. It is
installed at startup and must exist in the backend or startup fails;
`@handler("NAME", optional=True)` logs and skips a missing one instead (plugins). It
runs on an RFC server thread: return quickly and only call thread-safe
`SessionManager` methods.

**A static rule**: add a `(regex, message)` to `FORBIDDEN` in
`llm/audit.py`, and a case to `tests/test_audit_rules.py`. Remember that
literals are blanked in the text the rules see.

**A chat command**: extend `Orchestrator.COMMANDS` and `command()`; the
page needs no change unless a new event is introduced.

## Conventions

- Python 3.12, standard library first; dependencies are pinned in
  `pyproject.toml` (dotenv, FastAPI, uvicorn, openai, PyYAML; pytest, httpx
  and markdown as the `dev` extra). Install with `pip install -e .[dev]`.
- `config.NAME` is read at call time so tests can monkeypatch it.
- ABAP in `abap/` stays within 72 columns and 4.5B syntax; both releases
  compile the same sources.
- Every finding on a real system goes into `abap/README.md` ("Findings")
  with a date, and into 13 here.

## Languages and installation conventions

The code carries no language and no customer name. Everything that reads
a question by its words sits in [`lexicon.py`](../src/curie/lexicon.py)
as one dictionary per ISO language code: follow-up markers, question
openers, references, determiners for entity references, how-to wording,
program requests, month names, the suffix a year may carry, stop words,
detection markers and accent folding. `CURIE_LANGUAGES` selects the
active ones (English always); `memory/threads.py`, `memory/shape.py`,
`memory/evaluate.py`, `memory/glossary.py`, `archive/episodes.py` and
`memory/dictionary.py` build their patterns from it at import. Adding a
language is one entry there and a test.

Conventions of one installation are configuration, not code: the
composite document key users write (`KEY_PATTERN`, `KEY_ROLES`; the
value type `key` in `memory/shape.py`, whose parts take the configured
role names) and the codes to ignore in questions (`VALUE_STOP_CODES`).
`tests/conftest.py` pins the first installation's conventions (Polish,
the `number/year` agreement key) for the fixtures; a test that needs
other conventions calls `shape.configure(pattern, roles, stop_codes)`.
When an example is needed in code or documentation, use standard SAP
names (`KNA1`, `T001`, `GJAHR`) or invented neutral ones (`ZORDERS`).

## 4.5B specifics worth remembering

- One-character language keys: Polish is `L`, not `P`.
- `RFC_GET_STRUCTURE_DEFINITION_P` has no language parameter;
  `RFC_GET_FUNCTION_INTERFACE_P` uses `PARAMS_P`.
- `BP_JOB_ABORT` and `BP_JOBLOG_READ` are not remote-enabled; XBP 1.0 is.
- `WS_EXECUTE` truncates at 128 characters; `GUI_RUN` takes 255.
- `PARAMETERS` without `LOWER CASE` upper-case their value; ids are
  upper-case hex for that reason.
- Non-Unicode codepage 1401 (ISO-8859-2): Polish is fine, typographic
  characters are not.
