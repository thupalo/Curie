# 07 LLM and tools

## Client

[`llm/client.py`](../src/curie/llm/client.py) wraps the `openai` SDK's
`AsyncOpenAI` with a configurable `base_url`, so any endpoint that speaks
the chat completions API works. Verified with OpenRouter. `stream()` yields
a small vocabulary independent of the SDK types:

| Event | Payload |
|---|---|
| `delta` | streamed text |
| `tool_calls` | list of `{id, name, arguments}` accumulated from the stream |
| `done` | `{content, finish_reason}` |

`LLM_STREAM=0` switches to non-streamed requests, replayed as the same
events, for servers whose tool-call streaming is unreliable. `complete()`
is the non-streamed call used by the audit; it asks for a JSON object and
retries without that option when the server refuses it. Cancelling the task
that iterates the stream closes the HTTP connection.

## System prompt

Built per request in [`llm/prompts.py`](../src/curie/llm/prompts.py) from:

1. **Role**: Curie, SAP expert inside system/client/release for the user;
   today's date; answer language (`LLM_ANSWER_LANGUAGE`, default the logon
   language); code and identifiers stay in English.
2. **Dialect profile** by `SAP_RELEASE`:
   - `45B`: 72-column lines; no ABAP Objects, no `STRING`/`XSTRING`, no
     inline declarations, string templates, `&&`, host variables, `VALUE #`,
     `COND`, `FOR`, table expressions, `CAST`; `DATA ... OCCURS` style
     tables; `CONCATENATE`; no `LOOP AT ... ASSIGNING`.
   - `ECC6`: 255 columns, ABAP 7.0, report style preferred, avoid 7.40+.
3. **Output contract**: at most one ```` ```abap ```` block, a complete
   report starting with `REPORT zai_temp.`, output via `WRITE` only,
   read-only, no selection screen, every `SELECT` bounded (`UP TO n ROWS`
   with n ≤ `ABAP_MAX_SELECT_ROWS`, or a selective `WHERE`), the current
   function-module policy in words, and what happens on a syntax error.
4. **Tool guide**: what each tool does, the 512-character row limit, WHERE
   syntax with quoted literals, paging with `skip`, how to find tables and
   fields through `DD02T`, `DD03L`, `DD04T`, and to adjust rather than
   repeat a failing call.

## History

`session.messages` holds the OpenAI-style messages without the system
prompt. `trim_history()` keeps the most recent messages within
`LLM_HISTORY_MAX_TOKENS` (estimated at four characters per token) and never
separates an assistant tool-call message from its tool results. Automatic
messages (syntax feedback, program output, discard and stop notes) are
`user` messages that the page does not show.

## The turn loop

A user turn (`Orchestrator._turn()`) begins with `open_thread()`: the
follow-up decision, the thread reset or continuation, the light-path
flag, and the `user_message` event. That is the first thing that
happens after the prompt arrives, before the catalogue is consulted, the
router asks the model or any knowledge is retrieved, so the user's
bubble is in the browser at once even when a replay or the router takes
seconds. Then the catalogue replay is tried (not for follow-ups), and
otherwise `run_turn()`:

1. append the user message (the bubble is already shown; an automatic
   message gets none);
2. stream a completion with the tool schemas; forward `assistant_delta`;
3. on tool calls: append the assistant message with its calls, emit
   `tool_call`, run the tool in the client pool, emit `tool_result`, append
   the `tool` message (cut to `TOOL_RESULT_MAX_CHARS`, rows halved until it
   fits), and loop;
4. on a plain answer: append and emit `assistant_done`, return the text.

Errors from the endpoint appear as `notice` events. More than
`LLM_MAX_TOOL_ROUNDS` rounds end the turn with a warning.

**The light path.** A question about SAP in general or about Curie
("jak wyświetlić saldo klienta w SAP GUI", "co to jest konto zbiorcze",
"who are you"; [`memory/threads.py`](../src/curie/memory/threads.py)
`is_general`: how-to or explanation wording and nothing that names data
of this system: no agreement, date, quoted text or longer number, no
table or field name, no program request) is answered without tools,
with the system card as the only knowledge, a short-answer instruction
and `LLM_LIGHT_MAX_TOKENS` as the budget, without the retry on
truncation. The thread stays on the light path through its follow-ups
until one names data ("a teraz dla umowy 100/2005"), which brings the
tools and the full budget back. Four-character identifiers are tables
and transactions alike (KNB1, FD10); with the dictionary index in the
bundle only real tables count. `llm_user` and `llm_call` in the session
log carry `light: true`. `LLM_LIGHT_PATH=0` switches it off.

## Tools

All three run as RFC client calls with the RFC user. Results are compact
JSON with a `note` field for anything the model must know.

### `sap_read_table`

Arguments: `table`, optional `fields`, `where` (Open SQL without the word
WHERE), `max_rows` (default `READ_TABLE_DEFAULT_ROWS`, cap
`READ_TABLE_MAX_ROWS`), `skip`.

1. Deny list (`READ_TABLE_DENY`).
2. Structure from the DDIC cache (fields, key flags, types, lengths).
3. Field plan: with `fields`, validate and check the total width against
   512, error listing widths on overflow; without, key fields first, then
   position order while they fit, skipping `FLTP` and long `RAW`, and
   reporting omissions. Width estimates are conservative for packed
   numbers.
4. `OPTIONS` packing into 72-character lines that never split a token or a
   quoted literal; a list of conditions is joined with `AND`.
5. The call with `ROWCOUNT = max_rows + 1` to detect more rows. On
   `DATA_BUFFER_EXCEEDED` the last field is dropped and the call retried, up
   to three times.
6. Parsing by the `OFFSET`/`LENGTH`/`TYPE` the backend returned: dates to
   ISO, times to `hh:mm:ss`, integers, packed numbers (comma or point
   notation, trailing sign) as decimal strings.
7. Result cache for `READ_TABLE_CACHE_TTL` seconds.

Result: `{table, columns:[{name,type,text}], rows, row_count, skip,
truncated, note}`.

### `sap_get_structure`

`RFC_GET_STRUCTURE_DEFINITION_P` (fields, positions, types, lengths),
enriched from `DD03L` (key flag, data element, DDIC type, check table) and
`DD04T` (texts in `SAP_LANGU`, then English). Cached `DDIC_CACHE_TTL`.
Verified: MARA, 124 fields with Polish texts in 0.4 s.

### `sap_get_function_interface`

`RFC_GET_FUNCTION_INTERFACE_P` with `LANGUAGE = SAP_LANGU`; the parameter
table is `PARAMS_P` on 4.5B and `PARAMS` on newer releases. Returns
parameters with class, structure, field, type, length, default, optional
flag and text, plus the exceptions. Cached.

### `sap_get_domain_values`

Fixed values of a field's domain with their texts (`DD03L` → `DD04L` →
`DD01L` → `DD07T`, falling back to `DD07L`), or the check or value table
that holds the allowed values. Cached like the dictionary.

### `sap_knowledge`

Free-text search over Curie's notes (see 14). Present only when the
knowledge layer is enabled.

### `sap_find`

Tables and fields by words, from the dictionary index the consolidation
worker built (14): `query` in Polish or English, or a table or field
name; `kind` any, table or field. Answered locally in milliseconds with
the tables (text, class, usage count, copies and archives of the table
grouped under it) and the matching fields with exact names, types, key
flags and check tables; the fields programs use most come first. Offered
only when the index exists; the tool guide tells the model to use it
before reading DD02T or DD03L.

## Caches

Four caches share one helper ([`caches.py`](../src/curie/caches.py)): structures
(`ddic_structures`), function interfaces for the model
(`function_interfaces`), the interfaces Curie itself calls
(`rfc_interfaces`), and table read results (`read_table_results`). Each is
thread-safe, expires entries after its TTL (`DDIC_CACHE_TTL`,
`READ_TABLE_CACHE_TTL`), holds at most `CACHE_MAX_ENTRIES` entries with
least-recently-used eviction, and counts hits, misses and evictions.
`GET /healthz` shows the counters; `POST /admin/cache/flush` from the Curie
host empties all caches, for example after a transport changed a table or
the `Z_AI_PROG_CHECK_AND_EXECUTE` interface. Nothing is persisted across
restarts: a cold structure costs 0.1 to 0.4 s.

The dictionary caches (structures, function interfaces on both the tool and the
client side, domain values) are also kept on disk under `CACHE_DIR` when
`CACHE_PERSIST=1`, tagged with the SAP system, and loaded at startup with
their real age, so the first dialog after a restart does not pay for the
metadata again. They then live `CACHE_PERSIST_TTL` (seven days) instead of an
hour: dictionary metadata changes with transports, and `curie --cache purge`
or `POST /admin/cache/flush` drops it when it did. `curie --cache warm
--from-knowledge` prefetches every table the knowledge notes mention plus the
interfaces Curie relies on, and `POST /admin/cache/reload` hands the files to
a running Curie; `curie --cache stats` shows what is on disk. Table reads
never persist.

## Audit model

The security review is a separate non-streamed call with the auditor
prompt (rules 1 to 5: no database changes, no system access, no dialogs,
no sensitive tables, bounded reads) at temperature 0, returning
`{allowed, risk, violations, summary}`. An unavailable auditor counts as
"not allowed" with `unavailable: true`; `ABAP_AUDIT_BLOCKS` decides whether
that blocks the Run button.

The call is skipped when the program's shape equals one the reviewer
already allowed and the user confirmed ([`memory/audit_memo.py`](../src/curie/memory/audit_memo.py),
`ABAP_AUDIT_MEMO`): the key blanks string literals but keeps table
names, function names and numbers, and carries the version of the rules
(auditor prompt, static ruleset, audit model, deny list), so a changed
rule invalidates every stored verdict. Programs with dynamic names
(`FROM (lv_tab)`) are never memoised. The entries come from the archive
at start and from the running sessions; the card's summary says "same
program shape allowed on <date>, run <id>; review not repeated".

## Observed behaviour (nvidia/nemotron-3-super-120b via OpenRouter)

- Reads the field list from a tool error and corrects the next call.
- Writes 4.5B-style code (`DATA: BEGIN OF ... OCCURS 0`, `LIKE`) with the
  profile; occasionally guesses field names (`NAME1` on T000), which the
  SAP syntax check catches.
- Sometimes starts an answer with reasoning text; harmless.
- Typographic dashes and quotes in comments are transliterated before the
  syntax check.
