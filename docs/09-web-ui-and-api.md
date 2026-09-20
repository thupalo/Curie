# 09 Web UI and API

## Endpoints

All session endpoints take the session id from the path. Unknown ids are
404, closed sessions 410, requests from an address other than the bound one
403 (`SESSION_BIND_IP`).

| Method | Path | Purpose |
|---|---|---|
| GET | `/s/{id}` | The page (`static/index.html`); attaches the browser |
| GET | `/s/{id}/events` | SSE stream: `hello`, transcript replay, current `state`, then live events; `: keepalive` every 15 s |
| POST | `/s/{id}/prompt` `{text}` | Start a turn → 202; 400 empty, 413 over 20,000 characters, 409 while a turn runs. `/reset` or `/clear` as the text clears the conversation → 200 `{command: "reset"}`; `/remember <fact>`, `/forget <id>` and `/knowledge` are chat commands too (see 14) |
| POST | `/s/{id}/stop` | Cancel the turn or abort the running job → `{stopped}` |
| POST | `/s/{id}/runs/{run_id}/confirm` | Run the checked program → 202; 409 if nothing is ready |
| POST | `/s/{id}/runs/{run_id}/discard` | Drop it |
| POST | `/s/{id}/ping` | Liveness; returns the state |
| POST | `/s/{id}/close` | Window closed (`sendBeacon`) |
| POST | `/s/{id}/reset` | Same as the `/reset` command |
| GET | `/healthz` | `{ok, sessions, caches}` with entries, hits, misses and evictions per cache |
| POST | `/admin/cache/flush` | Empty every cache; accepted only from the Curie host itself (loopback) |
| POST | `/admin/knowledge/reload` | Take the compiled knowledge bundle from disk again (loopback; chapter 14) |
| POST | `/dev/open` `{prompt, user}` | Only with `CURIE_DEV=1`: a session without SAP GUI |

## SSE events

| Event | Data | Persisted for replay |
|---|---|---|
| `hello` | `session_id`, `sap{sysid, client, user, language, release}`, `initial_prompt`, `state`, `state_text` | first on every connect |
| `state` | `state`, `text` | no (sent after the replay) |
| `user_message` | `id`, `text` | yes |
| `assistant_start` | `id` | yes |
| `assistant_delta` | `id`, `text` | no |
| `assistant_done` | `id`, `text`, `interrupted` | yes |
| `tool_call` | `id`, `name`, `args` | yes |
| `tool_result` | `id`, `name`, `ok`, `summary`, `ms` | yes |
| `notice` | `level` (info, warn, error), `text` | yes |
| `code_ready` | `run_id`, `progname`, `source`, `syntax` (`{ok, message, line, word}` or `null` when not checked), `audit` (`{allowed, risk, violations, summary}` or `null`), `can_run` | yes |
| `syntax_error` | `run_id`, `message`, `line`, `word`, `attempt`, `max_attempts` | yes |
| `run_started` | `run_id`, `job`, `mode` | yes |
| `run_result` | `run_id`, `ok`, `rc`, `message`, `lines` | yes |
| `session_reset` | `text` | no |
| `session_closed` | `reason` | no (sent when closed) |

States: `CREATED`, `IDLE`, `THINKING`, `TOOL`, `CHECKING`, `CONFIRM`,
`RUNNING`, `CLOSED`.

## The page

[`web/static/`](../src/curie/web/static/): vanilla JS, no build step. `marked`,
`highlight.js` and `DOMPurify` are loaded from cdnjs with pinned versions
and the page degrades to plain text when they are unreachable. Model text
is inserted with `textContent`; markdown goes through marked, DOMPurify and
an allow-list sanitizer; links open in a new window so the app window never
navigates away.

Layout: header with the product name, SID/client/user/release, two dots
(SAP heartbeat green while the session is open; LLM pulsing red while the
model works) and the status text; the transcript with a "Latest" jump
button; the composer (Enter sends, Shift+Enter newline, Esc stops).

Cards: user and assistant messages (markdown), collapsible tool cards with
duration and result summary, code cards with line numbers, a column-72
ruler, syntax and audit badges, violations, Run and Discard, run status and
the list output with a copy button. Notices for retries, replaced
characters, infrastructure errors. A full-page overlay when the session
ends, with the reason in plain words.

Liveness: a ping every 3 s; `sendBeacon` to `/close` on `pagehide`; the
event stream reconnects on its own and the transcript is rebuilt from the
replay.

## Scripting against Curie

[`curie.tools.devchat`](../src/curie/tools/devchat.py) is a complete client: it opens a dev session,
sends a prompt, prints events, and with `--run` confirms the first runnable
code card and waits for the list. It is the reference for the event
handling above.
