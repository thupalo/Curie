# 15 Chat plugin: direct SAP → model calls

An optional part of Curie, off by default. With `CHAT_PLUGIN=1` the same
process, on the same Program ID `SAP_AI`, also serves four function
modules that ABAP programs call directly to talk to the model, without a
browser, without tools and without ABAP execution:

| FM | Purpose |
|---|---|
| `Z_BLA_RFC_CHAT_OPEN` | start one answer for a message history, get a handle |
| `Z_BLA_RFC_CHAT_READ` | take the words that arrived since the last call |
| `Z_BLA_RFC_CHAT_CLOSE` | release the handle, abort the stream if still running |
| `Z_AI_CHAT_COMPLETION` | one call, one complete answer (built on the three above) |

The first three are the streaming interface of transaction ZBLA (module
pool `SAPMZBLA`, function group `ZBLA`), specified in
`CHAT_INTERFACE.md` at the project root. The fourth is the interface the
dummy in function group `ZAICHAT` already had. The design and its
reasoning are in `CHAT_PLUGIN_PLAN.md`.

## Wire format

RFC on 4.5B carries flat CHAR fields that lose trailing blanks, so the
interface transports **words and line breaks**, not raw model deltas:

| Structure | Fields |
|---|---|
| `ZBLA_RFC_MSG` | `MSGNO NUMC 4`, `ROLE CHAR 9` (`user`, `assistant`, `system`), `LINENO NUMC 4`, `TEXT CHAR 255`. Rows with the same `MSGNO` are one message, lines joined with newlines |
| `ZBLA_RFC_CHUNK` | `TEXT CHAR 255` one word (at most 200 characters, longer words are split), `NEWLINE CHAR 1` a line break follows, `DONE CHAR 1` last chunk |
| `ZBLA_RFC_CTL` | `HANDLE INT4`, `MAX_CHUNKS INT4`, `DONE CHAR 1`, `ERRTEXT CHAR 80`: reference fields for the scalar parameters |

Runs of blanks collapse to one; `\r` and control characters are dropped;
the client joins words on a line with one blank. The last chunk of an
answer is an extra empty row with `DONE = X`. If the model stopped for
length or a content filter, the word `[truncated]` precedes it.

Text goes out through the same transliteration as generated ABAP (a dash
becomes `-`, quotes become ASCII) and then `replace` encoding to the
backend codepage, so a character outside ISO-8859-2 arrives as `?`
instead of failing the RFC conversion.

## Streamed calls

`OPEN` validates the history (at least one `user` message, and the last
one is a `user` message; otherwise `NO_USER_MESSAGE`), prepends
`CHAT_SYSTEM_PROMPT` when there is no `system` message, drops the oldest
messages until the history fits `CHAT_MAX_INPUT_CHARS`, and starts the
model stream. It returns before the first token. Beyond
`CHAT_MAX_HANDLES` open answers it raises `SERVICE_ERROR` "busy".

`READ` waits up to `CHAT_READ_WAIT` seconds for the first chunk, then
takes what is already there, up to `IV_MAX_CHUNKS`. An empty table with
`EV_DONE = space` means "nothing yet, call again", which is normal with a
reasoning model that thinks before the first visible word. After the last
chunk, `EV_DONE = X` and further calls return an empty table until CLOSE.
If the stream failed, every word received before the failure is delivered
first; the READ that finds nothing but the failure raises `SERVICE_ERROR`
with the text in `sy-msgv1`, so the client keeps the partial answer.

`CLOSE` never raises; an unknown handle is ignored. A handle nobody reads
for `CHAT_HANDLE_TTL` seconds is dropped by the server (`INVALID_HANDLE`
afterwards).

## The non-streamed call

`Z_AI_CHAT_COMPLETION` takes the history as `ZCHATMSG` rows (one row one
message, `MSGROL` `U`/`A`/`S`; anything else counts as user) plus
`I_PROMPT` as the new user message, opens a handle, reads until the
answer is complete, closes, and renders the chunks into `TLINE` rows:
lines wrapped at 132 characters before the word that overflows,
`TDFORMAT` `*` for a line that starts a paragraph, blank for a wrapped
continuation, `/` for an empty line. `E_MODEL` returns the logical model
name served, `E_TOKENS` an estimate (four characters per token) since
most streaming servers send no usage block.

`I_MODEL` is a logical name looked up in `CHAT_MODELS`; blank means the
first entry; unknown raises `MODEL_NOT_AVAILABLE`. A blank prompt raises
`EMPTY_PROMPT`. When the deadline `CHAT_SYNC_TIMEOUT` passes or the stream
fails after some output, the lines produced so far come back with
`[timeout]` or `[error]` as the last line; with no output at all the call
raises `MODEL_NOT_AVAILABLE` with the reason, the nearest exception the
interface declares. The call holds a dialog work process for up to the
deadline: it suits jobs and short answers, screens should use the
streamed calls.

## Capacity

A registered server instance is bound to the calling ABAP session's RFC
connection from the first call until `RFC_CONNECTION_CLOSE` or the end of
the session. A ZBLA conversation therefore occupies one of the
`RFC_REG_COUNT` registrations for its whole duration, like a Curie GUI
session does with its heartbeat loop. Hence:

- the ZBLA facade calls `RFC_CONNECTION_CLOSE` after every CLOSE;
- `CHAT_MAX_HANDLES` stays below `RFC_REG_COUNT` (`--check` warns);
- with the plugin on, set `RFC_REG_COUNT` to the expected Curie sessions
  plus the expected parallel chats plus one.

All registrations live in one process, so any of them can serve any
handle.

## Where the stream runs

By default as a task on Curie's asyncio loop through the shared
`LLMClient`: one HTTP client, one provider layer, and whatever changes
there (temperature handling, another provider) apply to the plugin too.
`CHAT_STREAM_THREADS=1` runs one thread with a client of its own per
answer instead, for complete separation from Curie's model path. Both use
the same service and pass the same tests.

## Operations

- `curie --check` lists the plugin modules as `(plugin)`; a missing one is
  a warning, not a failure, because at startup a missing module disables
  the plugin with a logged error and Curie serves the rest.
- `curie --selftest` runs a chat round when the plugin is on: from inside
  SAP through the destination, one streamed answer (chunk count, number
  of READs, first word) and one `Z_AI_CHAT_COMPLETION`.
- `/healthz` carries a `chat` block: open handles with caller, model, age
  and chunk count; counters `opened`, `completed`, `failed`, `expired`,
  `refused`, `sync_calls`.
- Log lines carry system, client, user and calling program, the number of
  messages and characters, never the text.
- Shutdown aborts every stream and wakes every READ in flight, so
  `server.stop()` is not held up by a waiting READ.

## Security

Whoever can call destination `SAP_AI` from ABAP can use the model; that
is an `S_RFC` decision on the SAP side. The plugin has no tools, no
knowledge injection and no ABAP execution. Limits: handles, input size,
answer tokens, handle TTL, deadline.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CHAT_PLUGIN` | `0` | `1` serves the four function modules |
| `CHAT_MODELS` | `ZGPT-45B=<LLM_MODEL>` | `LOGICAL=model,...`; the first entry is the default and serves the streamed calls |
| `CHAT_MAX_TOKENS` | `LLM_MAX_OUTPUT_TOKENS` | answer length cap |
| `CHAT_SYSTEM_PROMPT` | a short prompt naming system, client and language | prepended when the history has no system message; empty = none; `@file` reads a file; `{sysid}`, `{client}`, `{language}` are replaced |
| `CHAT_READ_WAIT` | `10` | seconds a READ waits for the first chunk |
| `CHAT_HANDLE_TTL` | `300` | seconds without READ before a handle is dropped |
| `CHAT_MAX_HANDLES` | `4` | concurrent answers |
| `CHAT_MAX_INPUT_CHARS` | `60000` | history size sent to the model |
| `CHAT_SYNC_TIMEOUT` | `60` | `Z_AI_CHAT_COMPLETION` deadline in seconds |
| `CHAT_STREAM_THREADS` | `0` | `1`: one thread and HTTP client per answer |

## SAP side

See [`abap/ZBLA_CHAT.md`](../abap/ZBLA_CHAT.md): the three structures, the
four function modules, the facade change in group `ZBLA` and the wrapper
for `Z_AI_CHAT_COMPLETION`.
