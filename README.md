# Curie — LLM chat for SAP over RFC

Curie is a Python service that puts an LLM chat inside SAP. A user starts
transaction `ZAI` in the SAP GUI, a Chrome window opens, and the model can
read tables and the dictionary through RFC, write ABAP reports, and run them
in SAP after a syntax check, a security review and the user's confirmation.
It works with **ECC 6** and newer, and even with **SAP R/3 4.5B**: a Unicode
system runs on NW RFC SDK 7.50, an old non-Unicode system needs NW RFC SDK
7.20 (see Requirements).

The full documentation is in [docs/](docs/README.md): architecture,
installation, configuration reference, SAP objects, session lifecycle, the
model and tools, the ABAP execution path, the web API, security, operations,
development, and status. The design it grew from is
[../CURIE_PLAN.md](../CURIE_PLAN.md); the SAP-side sources and notes are in
[abap/](abap/README.md).

```
SAP GUI: ZAI_START ──RFC──► Curie (registered server SAP_AI)
   │ starts Chrome --app=<URL>          │ FastAPI + SSE
   ▼                                    ▼
Chrome window ◄──────────────────► curie ──► OpenAI-compatible LLM
                                        │ RFC client calls
                                        ▼
                     RFC_READ_TABLE, RFC_GET_*_P, Z_AI_PROG_CHECK_AND_EXECUTE
                     background job ZAI_RUNNER ──RFC──► Z_AI_LIST_DELIVER
```

## Demo

https://youtu.be/Wvq-lqF_mdA
https://youtu.be/Mun0lyA23ro

## Requirements

| | |
|---|---|
| Python | 3.12 (Windows) |
| NW RFC SDK 7.50 (Unicode systems) or 7.20 (non-Unicode systems such as 4.5B) | `C:\nwrfcsdk` (`SAPNWRFC720_HOME`) — one SDK for both directions; never two generations in one process |
| SAP side | SM59 destination `SAP_AI` (type T, registered server), the `ZAIC` package (function group `ZAIC`, reports, transaction `ZAI`) from [abap/](abap/) |
| LLM | any endpoint with the OpenAI chat completions API (OpenAI, Azure, vLLM, Ollama, ...) |
| End-user PC | Chrome, network access to `WEB_PUBLIC_URL` |

The SDK comes from the SAP Support Portal (SAP Note 1025361) and is not in
this repository. 7.50 works well against Unicode backends (ECC 6 and
newer, S/4HANA). Against a non-Unicode backend such as R/3 4.5B only 7.20
works: a 7.50 server answers in Unicode, which the old kernel rejects with
`CALL_FUNCTION_FORMAT_NOT_SUP`, while 7.20 answers in the backend's
codepage and reaches back to 4.0B. The binding loads either one. pyrfc is
not needed.

## Setup

```powershell
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # fill in SAP logon, RFC_PROGID, LLM_*, WEB_PUBLIC_URL
curie --check     # logon, every FM interface Curie needs, the LLM
curie             # serve
python -m curie.tools.rfc_dev           # serve, restarted when a source file changes
```

`--check` prints the interface of every function module Curie calls or
serves as the backend actually has it, the `T_SOURCE` line width, the detected
release profile and `SAP_LANGU`, and a one-token LLM round trip.

`curie --selftest` (with Curie running) calls `Z_AI_SESSION_OPEN`,
`Z_AI_HEARTBEAT` and `Z_AI_SESSION_CLOSE` from inside SAP through the SM59
destination, six times. A "not found" there means a second server is
registered under the same Program ID.

## Operations

- **Windows service.** `service\install-service.ps1` installs Curie under
  NSSM (`-Uninstall` removes it). It runs `venv\Scripts\python.exe -m curie`
  from the project directory, restarts on failure, and stops with Ctrl+C so
  sessions close and the gateway registrations are released. Set
  `CURIE_LOG_FILE` in `.env` for a rotating application log.
- **One Program ID, one process.** Nothing else may be registered as
  `SAP_AI` at the gateway; `--selftest` catches it.
- **Session protection.** A session is bound to the IP of the first browser
  request (`SESSION_BIND_IP`, switch off behind a reverse proxy), a user may
  hold `SESSION_MAX_PER_USER` sessions, and `SESSION_MAX` caps the total.
- **Stop.** While the model works, Stop cancels the request. While a program
  runs in SAP, Stop aborts the job with `BP_JOB_ABORT` (when the backend
  offers it remotely) and deletes the program; otherwise the job finishes
  on its own and its late result is dropped.
- **`CURIE_DEV=1`** opens `POST /dev/open`, which creates sessions without
  the SAP GUI (`curie.tools.devchat` uses it). Never set it in production: anyone who
  reaches the port can then chat as the RFC user.
- **Function module policy** for generated programs: `ABAP_FUNCTION_POLICY`,
  `ABAP_FUNCTION_DENY`, `ABAP_FUNCTION_ALLOW` in `.env`; see `.env.example`.
- **Audit trail.** Every prompt, tool call, generated program, verdict and
  run is appended to `logs/sessions/<date>/<session>.jsonl`.

## How a session runs

1. `ZAI_START` pings the destination, calls `Z_AI_SESSION_OPEN` with the optional
   prompt, gets a URL, starts Chrome with `--app=<URL>`, then loops on
   `Z_AI_HEARTBEAT` every `SESSION_HEARTBEAT_SEC` seconds until Curie answers
   `CLOSED`.
2. The page streams the model's answer. Tool calls (`sap_read_table`,
   `sap_get_structure`, `sap_get_function_interface`) run as RFC client calls
   with the RFC user.
3. An answer with a ```` ```abap ```` block goes through the static rules
   ([llm/audit.py](src/curie/llm/audit.py)), a syntax check in SAP
   (`Z_AI_PROG_CHECK_AND_EXECUTE` mode `C`), and a model audit with a strict
   prompt. Syntax errors go back to the model automatically
   (`ABAP_FIX_RETRIES`).
4. The user presses **Run in SAP**. In `job` mode the function module does
   `INSERT REPORT` and schedules `ZAI_RUNNER` as the end user; the runner
   executes the program with `LIST TO MEMORY` and pushes the list to
   `Z_AI_LIST_DELIVER`. In `sync` mode the list comes back in the same call.
5. The list is shown and handed to the model for interpretation. The program
   is deleted afterwards; a sweep at startup removes leftovers.

Closing the Chrome window, a missing SAP heartbeat, the Stop button, and Curie
shutting down all cancel whatever is running; SAP's loop ends on the next
heartbeat.

Curie learns about your system from Markdown notes in [knowledge/](knowledge/README.md)
(tables, business terms, hints users add with `/remember`, programs users
confirmed) and from the dictionary itself (`sap_get_domain_values`). Chapter
14 of the docs describes it.

Typing `/reset` (or `/clear`) in the composer clears the conversation: the
model's history, the transcript and any code waiting for confirmation. The
session, its URL and the SAP heartbeat stay, so the user can start over
without leaving `ZAI`. A running job is aborted first.

## Configuration

Everything is read from `.env` by [src/curie/config.py](src/curie/config.py); see
[.env.example](.env.example) for the full list with defaults. The ones that
need a decision:

| Variable | Meaning |
|---|---|
| `RFC_*` | SAP logon and gateway registration (as before) |
| `SAP_RELEASE` | `ECC6` or `45B`; empty = autodetect. Chooses the ABAP dialect in the prompts and the 72/255 line width |
| `SAP_LANGU` | one-character `SY-LANGU` for `RFC_GET_*_P` (derived from `RFC_LANG`, `PL` → `L`) |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_AUDIT_MODEL` | the model endpoint |
| `WEB_PUBLIC_URL` | the URL SAP hands to Chrome; must resolve from the end-user PCs |
| `ABAP_EXEC_MODE` | `job` (background, as the end user) or `sync` |
| `READ_TABLE_DENY` | tables the tools and generated code may not read |
| `RFC_REG_COUNT` | gateway registrations; 3 keeps heartbeats and list deliveries from queueing |

## Layout

| Path | Role |
|---|---|
| [src/curie/cli.py](src/curie/cli.py) | entry point (`curie`, `python -m curie`): RFC server threads + uvicorn in one process, `--check`, `--selftest` |
| [src/curie/config.py](src/curie/config.py) | settings, release profiles, ISO → SAP language key |
| [src/curie/sapnwrfc720.py](src/curie/sapnwrfc720.py) | ctypes binding to the NW RFC SDK, 7.50 or 7.20: `Server` and `Client` |
| [rfc/client.py](src/curie/rfc/client.py) | connection pool, blocking and async calls, interface-aware `call_known` |
| [handlers/](src/curie/handlers/) | function modules served to SAP; `session.py` holds the `Z_AI_*` ones |
| [sessions/](src/curie/sessions/) | session model, state machine, `SessionManager`, JSONL audit log |
| [sap_tools/](src/curie/sap_tools/) | `RFC_READ_TABLE` with field packing, DDIC and function-interface caches, ABAP source folding, the executor |
| [llm/](src/curie/llm/) | OpenAI-compatible client, prompts, tool schemas, the turn loop, static + model audit, the code path |
| [web/](src/curie/web/) | FastAPI routes with SSE and the static page |
| [abap/](abap/) | SAP objects to install, with notes for 4.5B |
| [tests/](tests/) | unit and HTTP tests with fakes (`pytest -q tests`) |
| [src/curie/tools/](src/curie/tools/) | `phase0`, `devchat`, `rfc_dev` (reloading supervisor), `rfc_server` (the bare RFC server) |

## The RFC server underneath

Curie's server direction is the registered RFC server this repository started
as. The choice of SDK, the handler contract, parallel calls and
troubleshooting are unchanged:

- **Why ctypes and not pyrfc.** pyrfc 3.x is tied to SDK 7.50, whose server
  replies in Unicode (codepage 4103); a 4.5B kernel dumps with
  `CALL_FUNCTION_FORMAT_NOT_SUP`. The own binding runs on 7.50 for Unicode
  backends and on 7.20 for non-Unicode ones (it replies in codepage 1401 and
  reaches back to 4.0B); both directions use the one SDK loaded. Never load
  two SDK generations in one process.
- **Handlers.** Every module in `handlers/` is imported at startup; decorate
  a function with `@handler("FM_NAME")`. IMPORT/CHANGING/TABLES parameters
  arrive as keyword arguments (structures as dicts, tables as lists of dicts);
  the returned dict carries EXPORT/CHANGING/TABLES back. Raise
  `ABAPException("NAME")` for a declared exception. With `RFC_REG_COUNT > 1`
  handlers run concurrently.
- **Testing the link from SAP.** SM59 connection test → `RFC_PING`; SE37
  `STFC_CONNECTION` with the destination → echo; a report calling
  `STFC_STRUCTURE` → structures and tables.

| Symptom | Cause |
|---|---|
| `CALL_FUNCTION_FORMAT_NOT_SUP` in `ab_RfcSetHeader` | a 7.50-based server answering a pre-4.6C backend |
| `FU_NOT_FOUND` at startup | a served function module does not exist in the backend (create the `ZAIC` group first) |
| ABAP reports "program not registered" | Curie not running, or `RFC_PROGID` differs from SM59 |
| `Could not open the ICU common library` | `SAPNWRFC720_HOME` is wrong |
| Registration refused by the gateway | gateway ACL (`reg_info`, `gw/reg_no_conn_info`) |
| `DATA_BUFFER_EXCEEDED` in a tool result | the tool already retries with fewer fields; ask for specific fields |
| job finishes but no list arrives | the job could not reach `SAP_AI` (see the job log); check the RFC user's `S_RFC` and the destination |

Set `RFC_SERVER_TRACE=3` for SDK traces (`rfc*.trc` in the working directory).
