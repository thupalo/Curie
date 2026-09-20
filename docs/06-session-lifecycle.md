# 06 Session lifecycle

## Creation

`ZAI_START` calls `Z_AI_SESSION_OPEN`. The handler creates a `Session` with:

- `id`: 32 upper-case hex characters (128 bits). Upper case on purpose:
  the id travels through ABAP `CHAR32` fields and `PARAMETERS` without
  `LOWER CASE`, which upper-case their content.
- `sap`: system, client, user, language, kernel release from the connection
  attributes of the RFC call.
- `initial_prompt`: `I_PROMPT`, or the joined lines of `T_PROMPT`.
- state `CREATED`.

Limits: `SESSION_MAX` in total and `SESSION_MAX_PER_USER` per
(system, client, user); beyond them the call raises `SERVER_BUSY`.

The reply carries the id, the URL `WEB_PUBLIC_URL/s/<id>` and the heartbeat
interval.

## Browser attach

The first request from the page (`GET /s/{id}`, the event stream, or a
ping) attaches the browser: state `IDLE`, and with `SESSION_BIND_IP` the
client address is recorded; later requests from another address get 403.
Without an attach within `BROWSER_ATTACH_TIMEOUT` the session closes
(`attach_timeout`).

The page opens the event stream `GET /s/{id}/events`, which first replays
the transcript (`hello`, then persisted events, then the current `state`)
and then delivers live events. It pings every 3 seconds.

## Heartbeat

SAP calls `Z_AI_HEARTBEAT` every `SESSION_HEARTBEAT_SEC` seconds. The
handler records the time and returns the state word and status text:

| Session state | `E_STATE` | Typical `E_STATUS_TEXT` |
|---|---|---|
| CREATED, IDLE | `WAITING` | "Browser connected" |
| THINKING, CHECKING | `THINKING` | "Asking the model", "Security review of the code" |
| TOOL | `TOOL` | "Reading table MARA where MTART = 'FERT'" |
| CONFIRM | `CONFIRM` | "Waiting for confirmation in the browser" |
| RUNNING | `RUNNING` | "Job ZAI_13398EEC/23240101 running" |
| CLOSED | `CLOSED` | |

`ZAI_START` shows the text in the status bar and exits its loop on
`CLOSED` or on a failed call.

## Monitor

A task on the loop checks every second:

| Condition | Close reason |
|---|---|
| no heartbeat for `SESSION_HEARTBEAT_TIMEOUT` (not for dev sessions) | `sap_gone` |
| never attached for `BROWSER_ATTACH_TIMEOUT` | `attach_timeout` |
| attached, no ping for `BROWSER_TIMEOUT`, and no event stream open | `browser_gone` |
| IDLE or CONFIRM for `SESSION_IDLE_TIMEOUT` | `idle` |

Other close reasons: `browser_gone` from the page's `sendBeacon` on window
close, `sap_closed` from `Z_AI_SESSION_CLOSE` (also `CHROME_START_FAILED`
from `ZAI_START`), `shutdown`, `SELFTEST`.

## Closing

`SessionManager.close()` marks the session, cancels the running turn or
run task, cancels pending list deliveries, pushes `session_closed` to every
event stream, and logs the reason. The page shows a "Session ended" overlay
with a human explanation. The SAP loop ends on its next heartbeat. Closed
sessions stay in memory (their page still shows the overlay) until Curie
restarts.

## Stop

The Stop button (`POST /s/{id}/stop`):

- while the model works: cancels the turn task; the partial answer is kept,
  marked "interrupted", and stored in the history with a note;
- while a program runs in SAP: cancels the wait, aborts the job
  (`BP_JOB_ABORT`, or on 4.5B the XBP BAPIs), deletes the program, and
  reports "Stopped by the user" on the code card. If the job cannot be
  aborted it finishes on its own and its late delivery is dropped.

## Reset

Typing `/reset` or `/clear` (or `POST /s/{id}/reset`) cancels whatever runs,
aborts a running job, clears the model history, the transcript and pending
code cards, and returns to IDLE. The session, its URL, IP binding and
heartbeat stay. The page receives `session_reset` and empties its feed.

## A turn, in order

`POST /s/{token}/prompt` returns 202 at once; the turn runs as a task.
Its first act is `user_message`, emitted before anything that can take
time: the catalogue lookup, the router's model call, knowledge
retrieval, the model. A replayed report therefore shows the user's
bubble, then the "Report family ... replaying" notice, then the code
card; a generated answer shows the bubble, `assistant_start`, the
deltas, tool calls, `assistant_done`, and the code card when there is a
program. `state` events accompany the steps.

## Audit trail

Every session writes `logs/sessions/<date>/<session id>.jsonl`. Records
carry `ts`, `kind` and the event data: `session_open`, `browser_bound`,
`browser_attached`, `user_message`, `llm_user` (including automatic
messages), `llm_assistant`, `llm_interrupted`, `tool`, `code_checked`,
`syntax_error`, `audit`, `code_ready`, `code_confirmed`, `code_discarded`,
`run_started`, `list_delivered`, `run_result`, `run_aborted`,
`session_reset`, `notice`, `session_closed`; since September also
`llm_call` and `llm_done` (model, budget, notes in the prompt, tokens,
duration), `thread_outcome`, `feedback` with the voter, and `family` for
a replay. The first record's `ts` is the session's open time and orders
the logs. `curie --trace <session id prefix>` prints a log as a timeline
(chapter 11); the archive (chapter 14) is built from these files.
