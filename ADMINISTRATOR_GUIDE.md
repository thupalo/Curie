# Curie administrator guide

First draft, 11 September 2026. Sections 1 to 3 are placeholders by
agreement; sections 4 to 7 describe what the code does today. The
numbered chapters under `docs/` stay the reference for details; this
guide says what an administrator does, when, and with which command.

All commands below are run from the project directory
(`C:\...\SAP_AI_Chat\sap_ai_chat`) as `.\venv\Scripts\curie <options>`.
Options that belong to one command are listed under it; `-v` adds debug
logging to any of them.

---

## 1. Installation

To be written. Until then: `docs/03-installation.md`.

## 2. Initial technical parametrization

Mostly to be written. Until then: `docs/04-configuration.md` (every
`.env` setting with its default) and `.env.example`. The `.env` lives in
the parent folder of the project directory.

Three settings describe the installation rather than the technology and
must be set before users start, because the knowledge built later
depends on them:

- `CURIE_LANGUAGES`: the languages the users write in (ISO codes, for
  instance `pl,en`). Curie's word lists for follow-ups, general questions,
  month names and stop words are kept per language in
  `src/curie/lexicon.py`; a language that is not there yet is one
  dictionary entry to add.
- `KEY_PATTERN` and `KEY_ROLES`: the composite document key users write in
  questions, if the business has one. At the first installation
  agreements are written `100/2005` and stored as a zero padded number and
  a year: `KEY_PATTERN=\b(\d{1,7})/(\d{4})\b`, `KEY_ROLES=nrum:7,rokum:0`.
  The role names become the parameter names of report families, so they
  should be the field names people know.
- `VALUE_STOP_CODES`: upper-case codes that occur in questions but are
  never values of this system (column prefixes such as `TOW`, a
  registration prefix such as `DX`).

No table name, field name or language is written into the program code;
everything of that kind comes from these settings, from the notes and
from the dictionary index.

## 3. Knowledge bootstrap

To be written: resetting the contents, the primary hints, the rules and
the relations a new system needs before users start. Until then:
`knowledge/README.md` for the note format, `docs/14-knowledge.md` for how
the notes are used.

---

## 4. Starting a Curie instance

### 4.1 The service

```
.\venv\Scripts\curie
```

Starts everything in one process: the RFC server registered as
`PROGRAM_ID` at the SAP gateway, the client connection pool, the web
server for the browser windows, the caches, the archive, the knowledge
layer, and, when configured, the chat plugin and the in-process review
job. Stop with Ctrl+C, or through the Windows service
(`service\install-service.ps1`, chapter 11).

The start is fatal in three cases, each with exit code 2 and a clear
line at the end of the log: a configuration problem (`config:` lines),
the model endpoint not answering, or `LLM_MODEL` not served by the
endpoint. In the last case the log lists the names the endpoint serves;
put one of them into `.env` and start again. A Curie that cannot reach
its model does not register at the gateway, so SAP users see "no
server" instead of sessions that hang.

Startup order and what each line in the log means:

| Log line | Meaning |
|---|---|
| `backend TC0/005 release 45B ... -> profile 45B` | SAP logon works; the ABAP profile the model gets |
| `knowledge: N note(s) in ...; bundle ...` | notes loaded, the compiled bundle checked or rebuilt |
| `LLM ok: <model> at <url> replied 'OK'` | the model answered a one-token question |
| `caches from ...: ddic_structures=N, ...` | persisted dictionary caches loaded |
| `archive: ...\archive\curie.db` | the episode archive is open; sessions are imported when they close |
| `audit memo: N allowed program shape(s) from the archive` | programs of these shapes skip the security review (`ABAP_AUDIT_MEMO`) |
| `knowledge review every Ns` | the in-process review job is on (`KNOWLEDGE_REVIEW_INTERVAL`) |
| `chat plugin on: models ...` | the direct SAP-to-model chat is served (`CHAT_PLUGIN=1`) |
| `registered PROGRAM_ID=SAP_AI (3x) at gateway ...` | RFC server threads registered |
| `web on http://host:8765, public URL ...` | ready for browser windows |

One Program ID, one process: a second Curie (or a stale one) registered
under the same `PROGRAM_ID` takes calls it cannot answer.

### 4.2 Settings an administrator changes most

The full list is in `docs/04-configuration.md`. The ones that decide
how an instance behaves:

| Setting | Default | What it decides |
|---|---|---|
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | | the model endpoint; the name must be one the endpoint serves |
| `LLM_AUDIT_MODEL`, `KNOWLEDGE_REVIEW_MODEL`, `KNOWLEDGE_CONSOLIDATE_MODEL` | the main model | separate models for the security audit, the review job, the consolidation worker |
| `LLM_TOOL_CHOICE` | `auto` | how tools are offered; Curie steps down to `omit` and `off` on the server's 400s and says so |
| `RFC_REG_COUNT` | `3` | RFC server threads; each concurrent session needs one during its calls |
| `SESSION_MAX`, `SESSION_MAX_PER_USER`, `SESSION_IDLE_TIMEOUT` | `20`, `3`, `1800` | how many sessions, per user, and when an idle one closes |
| `SESSION_BIND_IP` | `1` | a session's browser must keep coming from the first address |
| `ABAP_REQUIRE_CONFIRM` | `1` | a program runs only after the user pressed Run |
| `ABAP_MAX_SELECT_ROWS`, the function policy | | what the static rules allow |
| `KNOWLEDGE_ENABLED` | `1` | the notes, families, glossary, card and dictionary in the prompt |
| `KNOWLEDGE_REVIEW_INTERVAL`, `KNOWLEDGE_REVIEW_APPLY` | `0`, `0` | the in-process review job and whether it applies safe fixes |
| `KNOWLEDGE_CARD_AUTO` | `0` | a generated system card goes into the bundle without a person's look |
| `ARCHIVE_ENABLED`, `ARCHIVE_DB` | `1`, `archive/curie.db` | the episode archive |
| `CACHE_PERSIST`, `CACHE_PERSIST_TTL` | `1`, 7 days | dictionary caches survive a restart |
| `CHAT_PLUGIN`, `CHAT_MODELS` | `0` | the direct SAP-to-model chat (chapter 15) |
| `CURIE_LOG_FILE` | | the rotating log file next to the console log |
| `CURIE_DEV` | `0` | `POST /dev/open` creates sessions without SAP GUI; development only |

### 4.3 Checks before and after a start

| Command | Does | When |
|---|---|---|
| `curie --check` | SAP logon, every function module interface Curie relies on, the served ones, the model | before the first start, after a transport, after changing `.env` |
| `curie --selftest [--dest D] [--rounds N]` | with Curie running: SAP calls the served functions and Curie answers | after a start, to prove the registration end to end |
| `GET /healthz` | sessions, caches, model check, knowledge bundle, chat plugin | any time; see section 6 |

---

## 5. Maintenance tasks

Three jobs read what the sessions left behind and make the next
sessions better. None of them changes a note a person confirmed without
being asked, none runs a program in SAP, and each writes a report next
to the notes.

### 5.1 Knowledge review

**What.** Every note not reviewed yet (a content hash in the note's
`review:` frontmatter remembers that) is checked three ways: the
dictionary check (tables and fields the note names must exist), the
similar notes of the same type, and one model verdict: `ok`, `fix` (with
a rewritten question for an exemplar whose question is a follow-up or a
programming request), `duplicate`, or `retire`. Verdicts land in the
note and in `knowledge/REVIEW.md`; a verdict nobody acted on stays
listed in every later report until applied or the note changes.

**When.** After a day with new sessions (new exemplars and hints), after
editing notes by hand, and before a consolidation. Roughly 60 to 90
seconds per note with a reasoning model, so a batch of 20 takes half an
hour; run it when the model is not busy with users, or in the
background with `KNOWLEDGE_REVIEW_INTERVAL`.

**Who.** The administrator, or the in-process job. Reading the report
and deciding on the flags that have no automatic fix is a person's
work: the knowledge owner.

The knowledge owners are also the people whose Correct / Wrong votes
should shape the notes once more users arrive. `KNOWLEDGE_FEEDBACK=all`
(the pilot) counts every vote; `owners` keeps the buttons for everyone
but lets only the SAP users in `KNOWLEDGE_OWNERS` (names or patterns
such as `THUPALO,KNOW*`) credit notes or put a program into the replay
catalogue, while everyone else's votes are logged and archived for the
review; `off` removes the buttons. Switch to `owners` before the first
group of ordinary users, because one "Correct" on a run result is enough
to make a program the answer for everyone who asks that question.

**How.**

```
curie --review-knowledge            # review pending notes, report only
curie --review-knowledge --apply    # also apply the safe fixes, including verdicts of earlier runs
curie --review-knowledge --all      # review every note again (many model calls)
curie --review-knowledge --no-sap   # without the dictionary check
```

`--apply` is a modifier of `--review-knowledge`; alone it does nothing.
What `--apply` does by itself: a question rewrite (once per note; the
note becomes `proposed` and leaves the catalogue, so check it and put it
back with `/catalogue add <id>` when it is right), retirements, the
retirement of the weaker duplicate, and the removal of table names the
dictionary does not know. Everything else in the report ("ungrammatical
phrasing", "ambiguous question") waits for a person: edit the note in
Obsidian, or `/forget <id>` in a chat.

The same job runs from the Curie host with `POST
/admin/knowledge/review` (`?apply=1`, `?all=1`), and every
`KNOWLEDGE_REVIEW_INTERVAL` seconds inside the service.

### 5.2 Consolidation

**What.** The background worker of the knowledge plan. From the notes
and the archive it builds: report families (confirmed programs of one
shape with typed parameters, written to `knowledge/families/`), the
glossary (business words to tables), the dictionary index (the customer
namespace and every table the notes or the archive mention, from SAP),
facts with evidence (joins, filter patterns, value forms, written into
the table notes' `facts:`), drafted table notes for tables that
confirmed programs read but nobody described, the pitfalls in order
(triggers from the archived syntax errors, duplicates merged, a
`counter_evidence:` count for pitfalls that were in the prompt when the
same error happened anyway, and the syntax error clusters with the ones
a deterministic repair now covers), a system card proposal, and the
compiled bundle the running Curie loads.

**When.** After the review, at least weekly, and after a transport that
changed customer tables (the dictionary index is rebuilt from SAP, about
a minute for 2000 tables). The model steps (naming families, the card)
take a few minutes; the rest seconds.

**Who.** The administrator runs it; the knowledge owner reviews what it
proposed: drafted table notes and families are `status: proposed` until
a person sets `confirmed` (or retires them), and a proposed family is
shown to the model as an example but never replayed.

**How.**

```
curie --consolidate                 # everything, with the model and SAP
curie --consolidate --no-llm        # families, glossary, dictionary, facts, bundle; no model calls
curie --consolidate --no-sap        # without the dictionary index
curie --consolidate --apply         # the generated system card goes into the bundle
```

The report is `knowledge/.build/consolidate.json`. A running Curie picks
the bundle's lexical parts up by itself; for the derived parts (card,
glossary, dictionary) call `POST /admin/knowledge/reload` from the host,
or restart. `knowledge/CARD.md`, written by a person, always wins over
the generated card.

Two things in the report deserve a look each time. `pitfalls.rule_candidates`
lists syntax errors that recurred in at least two episodes and have no
deterministic repair yet: a candidate for a repair in
`sap_tools/abap_source.py` or for a pitfall note. A pitfall with a high
`counter_evidence` (in `knowledge/insights/*.md` and in
`.build/pitfalls.json`) did not prevent the error it is about; retire it
or reword it.

### 5.3 Evaluation

**What.** The archive's confirmed episodes (a question and the program
the user accepted) are sent through the real turn, thread by thread:
the model, its tools, the static rules, the SAP syntax check, the audit.
Nothing runs (`ABAP_REQUIRE_CONFIRM` is forced on). Per case the report
says whether a family replayed it without the model, how many program
versions it took, whether the first passed the syntax check, whether
the final one is runnable, the audit verdict, whether its shape and
tables equal the accepted program's, whether the audit verdict was
reused from the memo, how many deterministic repairs were applied, and
the model and tool calls and seconds. `knowledge/EVAL.md` shows the
previous run beside the new one.

**When.** Before and after a change to prompts, notes, retrieval, the
bundle or the model, and as a periodic regression check. It costs about
a minute per case with a reasoning model and occupies the model; run it
outside working hours or with `--limit`.

**Who.** Whoever changes the prompts or the knowledge. The report is
the argument for keeping or reverting a change.

**How.**

```
curie --eval                        # every confirmed episode, newest first
curie --eval --limit 10             # the ten newest
curie --eval --cases <id>,<id>      # named episodes (ids from curie --archive stats or EVAL.md)
curie --eval --timeout 300          # seconds per case (default EVAL_TIMEOUT, 600)
curie --eval --include-general      # also the threads that opened with a general question
curie --trace <Log>                 # the timeline of one case; <Log> is the id in EVAL.md's Log column
curie --trace --newest eval         # the last case of the last run
```

A thread whose opening question was answered without asking SAP (no
tool call, no program before the first follow-up: "how do I see a
customer balance in SAP GUI", later turned to data) is excluded by
default. Such a case replays a general answer and a follow-up that
belongs to another question, costs minutes and measures nothing about
the data path. A case whose accepted program filters on identifiers or
dates the question never mentioned (the model took them from an earlier
thread) is excluded for the same reason: the question cannot reproduce
it. The report names the excluded ids with the reason;
`--include-general` replays them.

A question that asks for a program ("napisz program", "write a
program") is expected to produce one; a tools-only answer there counts
under "program asked for but none written". The system prompt tells the
model the same.

Evaluation sessions log under `logs/sessions/eval/` and never become
episodes; nothing an evaluation does reaches the notes.

### 5.0 Running the jobs as scheduled tasks

`service\install-tasks.ps1` registers four Windows scheduled tasks,
"Curie review", "Curie consolidate", "Curie eval" and "Curie
housekeeping", each running `python -m curie <options>` in the project
directory with its console output in `service\logs\task-<name>.log`.
Registered without `-Enable` the triggers are disabled and the tasks
are started by hand, which is the setting for a development host:

```
.\service\install-tasks.ps1                 # register, triggers disabled
.\service\install-tasks.ps1 -Start review   # or consolidate | eval | housekeeping
.\service\install-tasks.ps1 -Enable         # production: review daily 02:00, consolidate Sunday 03:00, housekeeping Saturday 04:00
.\service\install-tasks.ps1 -Remove
```

The eval task never gets a trigger. The tasks run under the account
that registered them (no password stored) and need the same `.env`, SAP
and model as the service.

### 5.4 Archive upkeep

**What.** The archive `archive/curie.db` holds every session as
episodes: question, follow-ups, model calls with what was in the prompt,
tool calls with results, program versions with verdicts, the run, the
feedback, the outcome. It is filled when a session closes; the CLI
back-fills or refreshes it from the logs.

```
curie --archive import              # logs not imported yet (idempotent)
curie --archive import --force      # everything again
curie --archive stats               # counts, outcomes, tokens, last import
curie --archive purge --last 2h     # drop the episodes of a test window (the logs stay)
```

### 5.5 Persisted caches

```
curie --cache stats                                   # what is on disk, how old
curie --cache warm --from-knowledge                   # prefetch the tables the notes mention
curie --cache warm --tables ZUMOWYL,ZFAKTURY
curie --cache purge [--names ddic_structures]         # drop the files
```

`POST /admin/cache/flush` empties memory and disk of a running Curie;
`POST /admin/cache/reload` takes the files again after a warm.

### 5.6 Chat commands with a maintenance role

Inside a session: `/knowledge` (what applies to the conversation, with
ids), `/remember <fact>` or `/hint <fact>` (a hint under the user's
name), `/forget <id>` (retire a note), `/catalogue` (the replayable
reports), `/catalogue add|drop <id>` (admit or remove an exemplar or a
family), and the Correct / Wrong buttons under answers and run results
(the utility signal; Correct under a run result admits the program to
the catalogue).

---

## 6. Monitoring, debugging, inspection

**Health.** `GET /healthz` on the web port returns JSON with the open
sessions, every cache's counters, the last model check (`llm`: ok,
model, endpoint, error, served names), the knowledge bundle (`knowledge`:
notes, families, glossary terms, dictionary tables, card source) and,
when on, the chat plugin's handles. `docs/11-operations.md` has the
`Invoke-RestMethod` lines.

**Logs.** The console and, with `CURIE_LOG_FILE`, a rotating file. `-v`
adds debug lines (every tool call with its duration, cache hits, bundle
rebuilds). Session logs are JSONL under `logs/sessions/<day>/<session
id>.jsonl`: one record per event, including per model call the model,
the settings, the note ids in the prompt and the token usage
(`llm_call`, `llm_done`), and every tool call with its full result. They
are the raw material of the archive and hold business data: treat them
like the database.

**Where to look for what.**

| Question | Where |
|---|---|
| What happened in a session, in order? | `curie --trace <session id prefix>` (or a file, or `--trace --newest` for the latest session, `--newest eval` / `--newest user` for one kind): one line per event with seconds, tokens, tools, repairs, syntax errors, the audit; `--full` for whole texts. `EVAL.md` lists each case's session in its Log column |
| What did the model get for this turn? | `llm_call` in the session log (note ids, sizes), `/knowledge` in the chat |
| Why was a program refused? | `code_ready` in the session log (`syntax`, `audit`), the card in the browser |
| What did Curie change in a program before the check? | The "Repaired before the check" notice, `repairs` in the `code_checked` log record |
| Was the audit skipped? | The card's summary says "same program shape allowed on <date>, run <id>; review not repeated"; `memo: true` in the `audit` log record; the "audit memo: N allowed program shape(s)" line at start |
| Which notes were credited for an answer? | `thread_outcome` in the session log (`used`, `unused`); the notes' `uses`/`successes`/`failures` |
| Which notes are flagged? | `knowledge/REVIEW.md`, each note's `review:` |
| What did consolidation produce? | `knowledge/.build/consolidate.json`, `knowledge/families/`, drafted `knowledge/tables/*.md` with `status: proposed` |
| Did a change help? | `knowledge/EVAL.md` |
| Which questions come back, which fail? | `curie --archive stats`; read-only SQL on `archive/curie.db` (`episodes`, `programs`, `tool_calls`, `facts`, `eval_runs`) |
| Is the model endpoint right? | `curie --check`; `/healthz` `llm` |
| Are tools on? | a warning at start and in the chat when the server refused them; `/healthz` |

**Debugging a turn without SAP GUI.** With `CURIE_DEV=1`, `POST
/dev/open` opens a session and `python -m curie.tools.devchat "<prompt>"`
drives it from the console (chapter 12).

**Notes in Obsidian.** Open `knowledge/` as a vault: the report files
link to the notes, `status`, `review` and `facts` are in the
frontmatter, and a saved edit reaches the running Curie within seconds.
The whole `knowledge/` folder except its `README.md` is ignored by the
Curie repository, like `archive/`, `cache/`, `logs/` and `service/logs/`:
what an installation learns and records never travels with the product
code. Back it up or version it on the installation's side (section 7).

---

## 7. Housekeeping

| Task | How | Notes |
|---|---|---|
| Test sessions left notes, reports and episodes behind | `curie --purge-knowledge --last 15m` (`--types exemplar,insight`, `--no-archive`, `--no-reports`, `--yes`) | moves the window's notes of every type and its `EVAL*`/`REVIEW` reports to `knowledge/.trash/<timestamp>/` and removes its archived episodes |
| A new SAP system, or a clean slate | `curie --purge-knowledge --all` | every note and report, the archive file and the bundle go to the trash; asks twice. `CARD.md`, `README.md` and the session logs stay |
| The same for the archive | `curie --archive purge --last 15m` | the logs stay |
| Session logs and the archive grow | `curie --housekeeping` (`--dry-run` first): log folders older than `LOG_RETENTION_DAYS` (180) are imported into the archive and removed, episodes older than `ARCHIVE_RETENTION_DAYS` (180) removed, the file compacted | facts and evaluation runs stay; 0 keeps everything; the "Curie housekeeping" task runs it weekly when enabled |
| Backup | copy `knowledge/` (without `.build/`) and `archive/curie.db` while no job runs; the archive can be rebuilt from the logs with `--archive import --force` as long as the logs exist | |
| Caches are stale after a transport | `curie --cache purge` then `--cache warm --from-knowledge`, or `POST /admin/cache/flush` | the dictionary index: `curie --consolidate --no-llm` |
| A note is wrong | edit it, or `/forget <id>`, or set `status: retired` with a `retired_reason` | never delete files a running Curie may read; retire |
| Stale programs in SAP | Curie sweeps `ZAI_<8 hex>` reports at start; `ZAI_RUNNER` and `ZAI_START` are never touched | |
| Upgrade | stop the service, `git pull`, `pip install -e .`, `curie --check`, start | chapter 11 |

To be added as the project grows: a restore procedure with a test, and
the retention of `service\logs`.
