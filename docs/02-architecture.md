# 02 Architecture

## Decisions as built

| | Decision | Why |
|---|---|---|
| D1 | One process, one SDK. The NW RFC SDK 7.20 is driven through ctypes ([`sapnwrfc720.py`](../src/curie/sapnwrfc720.py)) for both directions: the registered server and the client calls. | A 7.50-based server answers a 4.5B backend in Unicode and the backend dumps (`CALL_FUNCTION_FORMAT_NOT_SUP`). 7.20 speaks the old protocol and reaches back to 4.0B. Two SDK generations cannot share a process, so the client side had to move to 7.20 as well; pyrfc is gone. |
| D2 | FastAPI + uvicorn, Server-Sent Events, a single static page in vanilla JS. | Streaming is one-directional; SSE needs no extra infrastructure and reconnects on its own. |
| D3 | Generated programs run in a background job through a wrapper report; the list is pushed back over RFC. | `SUBMIT ... EXPORTING LIST TO MEMORY` and `VIA JOB` cannot be combined. `ZAI_RUNNER` runs the program with list-to-memory inside the job, converts it and calls `Z_AI_LIST_DELIVER`. A synchronous mode exists as a switch. |
| D4 | Code runs only after static rules, a syntax check, a model audit and the user's confirmation, in that order. | The user only sees code that compiles and carries a verdict; the syntax check is read-only so it may run before confirmation. |
| D5 | Sessions are created only by an RFC call from SAP; the URL carries a random 128-bit id. | Identity (system, client, user) comes from the RFC connection attributes; no credentials travel in parameters. |
| D6 | Release and language profiles. | `SAP_RELEASE` picks dialect rules and source width; `SAP_LANGU` is the one-character `SY-LANGU` the `RFC_GET_*_P` functions need on 4.5B. |

## Process and threads

Everything runs in one process started by [`src/curie/cli.py`](../src/curie/cli.py).

| Where | What runs there | Rules |
|---|---|---|
| RFC server threads, one per gateway registration (`RFC_REG_COUNT`, default 3) | The handlers in [`handlers/`](../src/curie/handlers/): `Z_AI_SESSION_OPEN`, `Z_AI_HEARTBEAT`, `Z_AI_SESSION_CLOSE`, `Z_AI_LIST_DELIVER`, plus `RFC_PING` and the `STFC_*` tests | Return fast. No RFC client calls, no awaiting. Touch the session manager only through its thread-safe methods. |
| asyncio loop, main thread | uvicorn/FastAPI, SSE streams, the session monitor, the orchestrator, the code path | Never call the SDK directly. |
| Client pool executor threads (`RFC_CLIENT_POOL`, default 2) | All RFC client calls: tools, syntax check, execution, job abort, cleanup | One SDK connection handle per thread at a time; the pool hands handles out. |

ctypes releases the GIL during SDK calls, so the server threads and the loop
coexist. The bridge between threads and the loop is
`SessionManager._on_loop()`, which uses `call_soon_threadsafe` when called
from a foreign thread.

Shutdown: uvicorn stops on SIGINT, SIGTERM or SIGBREAK; `src/curie/cli.py` then
closes all sessions (browsers get `session_closed`), stops the server threads
(in-flight calls finish, up to `RFC_DISPATCH_TIMEOUT`), and closes the pool.
The SAP loops end on their next heartbeat, which fails with "not
registered".

## Modules

| Path | Responsibility |
|---|---|
| [`src/curie/cli.py`](../src/curie/cli.py) | Entry point, wiring, `--check`, `--selftest`, logging, shutdown |
| [`config.py`](../src/curie/config.py) | `.env` reading with inline-comment stripping, release profiles, ISO to SAP language key, function-module policy |
| [`sapnwrfc720.py`](../src/curie/sapnwrfc720.py) | ctypes binding: marshalling of every RFC type, `Server` (registrations, dispatch, callbacks), `Client` (open, interface, invoke) |
| [`rfc/client.py`](../src/curie/rfc/client.py) | `SapClient`: pool of `Client`s, blocking and async calls, `call_known()` that drops parameters the backend lacks, interface cache |
| [`handlers/session.py`](../src/curie/handlers/session.py) | The four served `Z_AI_*` function modules |
| [`sessions/model.py`](../src/curie/sessions/model.py) | `Session`, `State`, `SapIdentity`, `AbapRun` |
| [`sessions/manager.py`](../src/curie/sessions/manager.py) | `SessionManager`: creation, heartbeat, close, reset, events, subscribers, monitor, JSONL audit log |
| [`sap_tools/read_table.py`](../src/curie/sap_tools/read_table.py) | `RFC_READ_TABLE` with field planning, OPTIONS packing, parsing, retries, cache |
| [`sap_tools/ddic.py`](../src/curie/sap_tools/ddic.py) | Structure definitions with key flags and texts from DD03L/DD04T |
| [`sap_tools/function_interface.py`](../src/curie/sap_tools/function_interface.py) | Function module interfaces |
| [`sap_tools/abap_source.py`](../src/curie/sap_tools/abap_source.py) | Fenced-block extraction, REPORT injection, line folding, codepage sanitising |
| [`sap_tools/abap_exec.py`](../src/curie/sap_tools/abap_exec.py) | Syntax check, execution (job or sync), delivery wait, job status, abort, cleanup |
| [`llm/client.py`](../src/curie/llm/client.py) | OpenAI-compatible streaming client |
| [`llm/prompts.py`](../src/curie/llm/prompts.py) | System prompt, dialect profiles, auditor prompt, fix and result prompts |
| [`llm/tools.py`](../src/curie/llm/tools.py) | Tool schemas and the dispatcher |
| [`llm/orchestrator.py`](../src/curie/llm/orchestrator.py) | One turn: stream, tool loop, cancellation, chat commands |
| [`llm/audit.py`](../src/curie/llm/audit.py) | Static ruleset and the model review |
| [`llm/abap_flow.py`](../src/curie/llm/abap_flow.py) | The code path from answer to result, confirm, discard, stop |
| [`web/app.py`](../src/curie/web/app.py) | HTTP endpoints and the SSE stream |
| [`web/static/`](../src/curie/web/static/) | The page: `index.html`, `app.js`, `style.css` |

## Data flow of one question

1. Browser `POST /s/{id}/prompt` → `Orchestrator.start_turn()` creates the
   turn task.
2. `run_turn()` builds `[system prompt] + trimmed history`, streams the
   completion, forwards `assistant_delta` events.
3. Tool calls are executed through `ToolBox.dispatch()` in the client pool;
   each result is appended as a `tool` message and the loop continues (at
   most `LLM_MAX_TOOL_ROUNDS`).
4. The final answer is stored and emitted as `assistant_done`; if it holds a
   ```` ```abap ```` block, `AbapFlow.handle_answer()` takes over (see 08).
5. The turn task ends; the state returns to IDLE, or CONFIRM when code is
   waiting.

## State machine

```
CREATED ──browser attached──► IDLE ──prompt──► THINKING ──tool call──► TOOL ──► THINKING
   │                          ▲                  │ ```abap block
   │ attach timeout           │                  ▼
   │                          │        CHECKING (rules, syntax, audit) ──error──► THINKING (auto-fix, ≤N)
   │                          │                  │ ok
   │                          │                  ▼
   │                          │◄──discard──  CONFIRM ──run──► RUNNING ──list / timeout / stop──► IDLE
   ▼                          │
CLOSED ◄──────────────────────┴── browser gone | SAP heartbeat timeout | Z_AI_SESSION_CLOSE | idle | shutdown
```

The heartbeat reply maps these to `WAITING`, `THINKING`, `TOOL`, `CONFIRM`,
`RUNNING`, `CLOSED` plus a status text, which `ZAI_START` shows in the GUI
status bar.
