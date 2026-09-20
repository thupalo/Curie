# 14 Knowledge: what Curie learns about the system

Curie's memory is a folder of Markdown notes, [`knowledge/`](../knowledge/README.md),
that describe the business and technical structure of the SAP system beyond
the dictionary: what a custom table holds, what a business term maps to,
what users told Curie, which programs users confirmed, which pitfalls
Curie ran into, and which reports are trusted enough to be replayed. It is
stages one and two of the learning design (see the analysis in the session
of 6 September, after FlowEvo's three layers): name-keyed retrieval, no
embeddings, humans in control.

## Where facts come from

| Source | Form | Trust | How it gets in |
|---|---|---|---|
| The data dictionary | `sap_get_structure`, `sap_get_domain_values` | facts | on demand, from SAP itself |
| Curated notes | `tables/*.md`, `concepts/*.md` | facts | written by people, or extracted from documents and reviewed |
| User hints | `hints/*.md` | "a user said" | `/remember <fact>` in the chat |
| Confirmed programs | `exemplars/*.md` | examples | written by Curie after a confirmed run succeeded |
| Pitfalls | `insights/*.md` | rules | distilled by Curie from a failure that was fixed, or written by people |
| The report catalogue | `exemplars/*.md` with `catalogue: true` | trusted programs | the user marked a run result correct, or `/catalogue add` |

The dictionary source needs no notes at all: `sap_get_domain_values` reads
the fixed values of a field's domain with their texts from `DD07T` (or the
check or value table that holds the allowed values), so "what does
STATUS 14 mean" is answered from SAP and stays correct after transports.

## Note format

Obsidian-compatible Markdown with YAML frontmatter; the folder can be
opened as a vault and edited there. It belongs to the installation, not
to the product: everything under `knowledge/` except `README.md` is
git-ignored in the Curie repository (notes of every type, families,
reports, the card, the trash, the bundle), so version it or back it up
with the installation's own means, next to `archive/` and `logs/`.

```yaml
---
type: table             # table | concept | hint | exemplar | insight
name: ZUMOWYL
aliases: [umowa leasingowa, umowy leasingowe, leasing agreement]
tables: [ZUMOWYL]       # SAP names the note is about
keywords: [leasing]
status: confirmed       # proposed | confirmed | retired
source: user:THUPALO    # provenance
created: 2026-09-06
uses: 0                 # maintained by Curie
successes: 0
failures: 0
---
Lease agreement data is located in table ZUMOWYL. An active agreement is
one whose STATUS is in the range 14 to 19; see [[active-agreement]].
```

Exemplars carry `question:` in the frontmatter and the program in an
```` ```abap ```` block in the body, plus `slots:` (the question's numbers
and quoted values that occur in the program), `audit:` (the verdict when
the user confirmed it) and `catalogue:`. Insights are one line of body,
an instruction; `status: proposed` until they helped twice.

## The facade, the bundle and the system card

The runtime talks to knowledge through one object,
[`memory/knowledge.py`](../src/curie/memory/knowledge.py) `Knowledge`:
`context_for(question, tables, extra)` returns the knowledge block for a
model call together with the ids of the notes and exemplars in it (the
`llm_call` log record and the archive get exactly that), and
`match_family(question)` returns a replayable template for the question
(today the catalogue's literal match; typed report families come with
phase K3 of `KNOWLEDGE_PLAN.md`). Everything else (`add_hint`,
`add_exemplar`, `record`, `retire`, `search`, ...) is delegated to the
store, so the notes stay the source of truth.

The facade also owns the **compiled bundle**, `knowledge/.build/`
(`KNOWLEDGE_BUILD_DIR`, git-ignored). Its lexical parts, `index.json`
(identifiers and word prefixes to note ids), `co_read.json` and
`templates.json`, are rebuilt in-process whenever a note changes, so
editing in Obsidian still takes effect within seconds. Its derived parts,
`card.md`, `glossary.json`, `families.json`, `graph.json` and
`pitfalls.json`, are written by the consolidation worker (later phases)
and stay as they are until it runs again; `POST /admin/knowledge/reload`
takes them from disk without a restart, and `GET /healthz` shows what the
bundle holds under `knowledge`. A missing bundle is never an error.

Two derived parts already act in the prompt: the **system card**, one
page about the system injected into every model call, is
`knowledge/CARD.md` when a person wrote one (the store does not treat it
as a note) and otherwise the bundle's `card.md`; and the **glossary**
maps business terms to tables, so a question that says "polisa" pulls
the ZUBEZP notes in before the model has named the table.

## Retrieval

Deterministic, in [`memory/store.py`](../src/curie/memory/store.py):

1. Context = the user's last prompt, the tables touched by tools in this
   session, and the model's previous answer in the same turn (so a table the
   model discovered through a tool pulls its note into the next round).
2. Upper-case identifiers in the context match `name` and `tables` exactly
   (3 points each). Aliases match when every word of the alias occurs in
   the context by its first five characters (2 points), which handles
   Polish inflection ("umowy leasingowe" matches "umów leasingowych").
   Keywords score 1. Notes wikilinked from a matched note come along with
   half a point.
3. At most `KNOWLEDGE_MAX_NOTES` notes within `KNOWLEDGE_MAX_CHARS`, tables
   first; at most `KNOWLEDGE_EXEMPLARS` exemplars, ranked by table overlap
   and question similarity, fewer failures first. Retired notes never
   appear.
4. The result is rendered into the system prompt as "Facts about this
   system (curated)", then "Hints users gave earlier (verify against the
   dictionary when in doubt)", then "Programs confirmed earlier for similar
   questions", and the model is told which is which.

The folder is re-read when a file changes (checked at most every three
seconds), so edits in Obsidian take effect on the next model call without
a restart.

## Commands and tools

| | Does |
|---|---|
| `/remember <fact>`, `/hint <fact>` | Stores a hint with the user's name, the date and the tables used so far in the session; the fact also goes into the current conversation |
| `/knowledge` | Lists the notes that match the current conversation, with their ids |
| `/forget <id>` | Retires a note (the file stays, marked `status: retired`) |
| `/catalogue` | Lists the replayable reports; `/catalogue add <exemplar id>` and `/catalogue drop <id>` maintain it |
| Correct / Wrong buttons | Under every answer and every successful run result: the utility signal (see "Feedback") |
| `sap_knowledge` (tool) | Free-text search over the notes, for the model when a term is unfamiliar |
| `sap_get_domain_values` (tool) | Domain fixed values with texts for a table field or a domain |

## Learning from runs

After a confirmed program ran successfully, Curie:

- keeps it as an exemplar (`exemplars/<date>-<run id>.md`) with the
  question that produced it, unless one with the same question wording
  already exists. The question is the thread's business question, not
  the last prompt: a prompt that refines the previous one ("Wygeneruj
  jeszcze raz poprawiając problem", "Pomiń kolumnę TOW*", "dla tych
  danych", "again", "instead"; see `memory/threads.py`) is a follow-up,
  and a later run in the same thread updates the thread's exemplar instead
  of adding one. A reference to an entity ("dla tej umowy", "tego
  klienta") counts as a follow-up only when the thread's question or
  answers mentioned that entity; otherwise the topic changed and a new
  thread starts, so a depreciation question after a how-to about SAP GUI
  balances is not filed under the how-to. With follow-ups the model rewrites the question with the
  refinements folded in (`refinements:` keeps the originals) and may
  propose a preference worth keeping as a hint, such as "leave out the
  TOW* columns"; the notice names it and `/forget` drops it;
- credits every note and exemplar that was in the prompt with a success.

After a failed run, the injected notes and exemplars are debited; an
exemplar with two failures and more failures than successes is retired
automatically. `KNOWLEDGE_CAPTURE_EXEMPLARS=0` switches capture off.

## The three layers

**Exemplars** (layer two in FlowEvo's terms, built first because it
transfers directly): confirmed programs injected as examples. Since stage
two the prompt also carries a data insight computed from them, no model
involved: "Confirmed programs that read ZUMOWYL also read ZKLIENT (2 of 3)".

**Insights** (layer three, pitfalls): when a program went through a syntax
or static-rule fix round and then ran successfully, Curie sends the failure
and the lines that changed to the model once and asks for one sentence of
general advice, or NONE. Fail-closed: no JSON, NONE, or a sentence shorter
than twelve characters means nothing is learned; a rule people retired is
never learned again. The result is an `insights/` note with `status:
proposed`, injected under "Pitfalls and rules learned on this system" and
flagged unconfirmed until it was in the prompt of two successful runs.
At most `KNOWLEDGE_MAX_INSIGHTS` per prompt; a rule without tables or
keywords is a general one and is always a low-priority candidate. People
can write insights too: a note in `insights/` with `status: confirmed`.

**The report catalogue** (layer one, template replay) needs care because
Curie has no gold answer to compare against. Admission is the user's:
a successful run whose result the user marked correct (or `/catalogue
add`). The exemplar then becomes a template: the numbers and quoted values
of its question that occur in its program are its slots. When a new prompt
has the same question signature and the same number of values, Curie
replays the program with the values substituted, without calling the
model, and says so in a notice. The replayed program still goes through
the static rules, the syntax check, the audit (the stored verdict is
reused only when no value changed) and the Run button; identifiers are
never slots, so a template can never be steered to another table. The
gain is consistency: the same business question yields the same reviewed
report every time.

## Feedback

Correct / Wrong under an answer is the outcome of the *thread* (the
question and its follow-ups), counted once: the first verdict on one of
its answers, or the result of a program run in it, decides. Only the
notes the thread's answers or programs actually used are credited or
debited: cited by id, a table of theirs read, a keyword of a pitfall or
hint present; the notes that were in the prompt but not used get a use
without an outcome, so a note that is retrieved often and helps rarely
shows it in its counters. A later vote in the same thread is
acknowledged but not counted. Correct under a run result puts the program
into the report catalogue; Wrong retires the exemplar Curie kept from
that run. For a replayed report, Correct credits the template and Wrong
debits it; two failures retire it. The session log records each counted
outcome (`thread_outcome`, with the used and unused ids).

Who may teach is a setting, `KNOWLEDGE_FEEDBACK`. `all` is the pilot
behaviour above. `owners` keeps the buttons for everyone but only the SAP
users matching the `KNOWLEDGE_OWNERS` patterns (`THUPALO,KNOW*`) change
notes or admit a program to the catalogue; anyone else gets "Noted,
thanks", the vote is logged with `counted: false` and archived, and their
own session still learns that the last answer was wrong. `off` hides the
buttons and the endpoint declines. The page learns the mode from the
`hello` event.

## The review job

`curie --review-knowledge` (standalone, needs the model and, for the
dictionary check, SAP), `POST /admin/knowledge/review` from the Curie
host, or the in-process job every `KNOWLEDGE_REVIEW_INTERVAL` seconds.
For every note not reviewed yet (a content hash in `review:` remembers
that; `--all` reviews everything again) it does three things:

1. **Dictionary check**, deterministic: every table the note names must
   exist in `DD02L`, and names the body presents as fields (`ZUMOWYL-STAN`,
   "field STAN", "kolumna STAN", "STAN in ZUMOWYL") must exist in `DD03L`.
2. **Similar cases**, deterministic: notes of the same type sharing tables
   and words, up to five, handed to the model.
3. **The model's verdict**, one call per note: `ok`, `fix` (an exemplar
   whose question is a follow-up or a pronoun reference gets the question
   the program answers; a vague fact gets its problem named),
   `duplicate` of a similar note, or `retire`. Fail-closed: no JSON means
   not reviewed, and the note is looked at again next time.

The verdict lands in the note's frontmatter and in `knowledge/REVIEW.md`
(with wikilinks, so Obsidian shows the flagged notes). A verdict that was
not acted on stays listed in every later report, marked "from an earlier
run", until it is applied or the note changes; a run without `--apply`
loses nothing, and `--apply` on a later run applies the stored verdicts
without asking the model again. With `--apply` (or
`KNOWLEDGE_REVIEW_APPLY=1` for the job) the safe actions are taken: the
rewritten question is applied and the note becomes `proposed` with the
old question kept under `refinements:`, the duplicate with fewer
successes is retired, a note the model wants retired is retired. Nothing
is deleted, and a retired note can be reopened by editing its status.

## Report families

The generalised unit behind the catalogue (plan 5.3): a confirmed program
with typed holes. [`memory/shape.py`](../src/curie/memory/shape.py) reads
a program's *shape* (tables, filtered fields and operators, aggregates,
joins, with literals masked) and binds every literal in a WHERE condition
to its field, also through variables (`lv_rokum = '2005'`). The typed
values of the question (the installation's document key when
`KEY_PATTERN` is set, for instance an agreement written `100/2005`,
a year, a month or a period in Polish or English, a quoted text, a code
such as `CARC`, a number) are matched against those literals: a literal
that renders one of them is a parameter, the rest are constants
(`anulowana = space`, `grupa_kod = 'UMLEA'`).

[`memory/families.py`](../src/curie/memory/families.py) clusters the
confirmed exemplars by shape, keeps as members of one family only the
programs whose logic is the same (output statements, comments and
variable names aside), and writes a note per family to
`knowledge/families/`:

```yaml
---
type: family
name: zumowyl-count-rokum
aliases: ["Ile umów zawarto w {year}?", "How many agreements were concluded in {year}?"]
tables: [ZUMOWYL]
params: {rokum: {type: year, role: year, field: ZUMOWYL.ROKUM, op: "=", literal: "'2005'"}}
family: exemplars/20260907-9F19ABA5     # the canonical member
status: proposed                         # confirmed when a member is in the catalogue
---
```

The body carries the member links and the template with `{{rokum}}`.
Inside texts the value as the question wrote it becomes a `{{name:text}}`
hole ("umowa 339/2005" prints the new question's "28/2005"), a bare
literal a `{{name:bare}}` hole. Before a replay the filled program is
checked for values of the canonical question that survived in its texts;
a template that leaks (built by an older rule) is rebuilt from the
canonical member on the spot and saved, and when even that leaks, the
family is not replayed and the log says why.
A family is *confirmed* (replayable) when its canonical member is in the
report catalogue, or when a person sets the status or uses `/catalogue
add families/<id>`; a *proposed* family is only shown as an exemplar for
a matching question. Matching a question needs both: its typed values
fill every parameter, and its masked wording is close to one of the
family's intents (word-prefix Jaccard of 0.5, or the same literal
signature), and it carries at most two content words the closest intent
does not have: more of them are conditions the family cannot apply ("na
pojazdy marki Ford z województwa śląskiego" against "ile umów zawarto w
{year}"), and the question goes to the model with the family as its
exemplar. Identifiers never become parameters, so a template can never
be steered to another table. The evaluation counts a replay whose shape
differs from the accepted program as "replayed with a different shape". The replay goes through the static rules,
the syntax check, the audit and the Run button like any program; the
notice names the family and the values; Correct and Wrong credit or
debit the family note.

When no family matches lexically, the model acts as a router
(`KNOWLEDGE_ROUTER`, plan 8.3): the families that share a table (from
the question or the glossary) or a word with the question, and whose
parameter types the question's values can fill, are listed with their
intents and parameters, and the model picks one or none and copies the
values from the question. Fail-closed: the pick must be one of those
candidates, every value must occur in the question and go through the
typed extractor, and the template must fill; otherwise the question goes
to the model as before. One short call on `KNOWLEDGE_ROUTER_MODEL`, only
when candidates exist; the notice then says the model matched the
question, and the log's `catalogue_replay` carries `routed: true`.

## The dictionary index and `sap_find`

The logs showed the model browsing DD02T and DD03L through
`sap_read_table` (the most error-prone tool pattern) and describing the
same tables session after session. The consolidation worker, when SAP is
reachable, reads the whole customer namespace (Z and Y tables and views
from DD02L) plus every standard table the notes or the archive mention:
table texts in the logon language and in English from DD02T, fields with
texts, types, key flags and check tables through the persisted structure
cache. The result is the bundle's `dictionary.json`
([`memory/dictionary.py`](../src/curie/memory/dictionary.py)).

Two things use it. The tool `sap_find(query, kind)` is offered to the
model as soon as the index exists: words in Polish or English ("polisa",
"numer rejestracyjny", "invoice date"), a table name or a field name,
matched by 5-character prefixes against table and field texts, answered
in milliseconds without SAP, with the tables, their texts and the
matching fields (exact names, types, key flags). And `context_for`
renders the tables in play (from the question, the glossary and the
retrieved notes) as compact dictionary lines, keys first, so a known
table costs no structure call at all. `curie --consolidate --no-sap`
skips the step; `GET /healthz` shows the number of indexed tables.

## Facts with evidence

The programs users confirmed carry facts nobody wrote down: which field
joins which, which constants a valid document needs, how a value is
written in a table. [`memory/facts.py`](../src/curie/memory/facts.py)
mines them deterministically from the program shapes (the parameters of
a report family are left out, they are values, not facts):

| Predicate | Example |
|---|---|
| `joins` | `ZKNA1.KUNNR joins ZUMOWYL.KLIENT` |
| `filter_pattern` | `ZFAKTURY.ANULOWANA is filtered with = space`, `ZMSTAN.GRUPA_KOD is filtered with = 'UMLEA'` |
| `value_form` | `ZRECZNE.NRUM takes an agreement number zero padded to 7 characters (the user writes 100/2005)` |

Each fact carries its evidence (exemplar ids and run ids), the number of
distinct programs and users, and a status: `proposed` when mined,
`verified` when every name exists in the dictionary index,
`contradicted` when the table is indexed but the field is not (the fact
is then neither rendered nor drafted), `confirmed` when two independent
programs show it or a person confirms it. The facts live in the
archive's `facts` table (a status set by a person is never overwritten)
and are rendered into each table note's `facts:` frontmatter, which the
prompt shows under the note as one line per fact; the note's prose is
never touched.

The same step drafts a table note for every table that confirmed
programs or tool calls used at least three times and that has no note
(the review found five such tables): the dictionary text, the key, the
fields programs use with their texts, the tables read together with it,
its facts, and glossary words as aliases. The draft is `status:
proposed` and says so in the prompt ("drafted by Curie, not yet
reviewed"); edit it in Obsidian and set `confirmed`, or retire it.

## The consolidation worker

`curie --consolidate` is the background process of the plan (section 7),
run by hand or on a schedule, never on a user's turn:

1. families: clusters and writes the family notes; with the model, one
   call per new family writes its Polish and English intent and a
   description (rejected unless every parameter's placeholder appears);
2. glossary: the words of confirmed questions aligned with the tables of
   the programs that answered them, from the exemplars and the archive's
   successful episodes, into the bundle's `glossary.json` (a word that
   co-occurs with a table in at least two episodes and in at least 60
   percent of its occurrences; entries a person marked `confirmed` stay);
3. the dictionary index (above), when SAP is reachable and not `--no-sap`;
4. facts: mined from the confirmed programs, verified against the index,
   saved to the archive, rendered into the table notes; table notes
   drafted for used tables without one;
4a. pitfalls ([`memory/pitfalls.py`](../src/curie/memory/pitfalls.py),
   plan P7), deterministic: the archived syntax errors are clustered by
   word and message; an insight whose keywords name such a word gets it
   as `trigger:`; insights that say the same thing (word-prefix Jaccard
   0.6) are merged into the one with the better record, the other
   retired with `superseded_by`; `counter_evidence:` counts the episodes
   where the pitfall was in the prompt and its trigger's error happened
   anyway. The bundle's `pitfalls.json` carries the pitfalls and the
   clusters, each marked whether a deterministic repair
   (`abap_source.repair_source`) now covers it; a cluster seen in two or
   more episodes without a repair is reported as a rule candidate;
5. the system card: with the model, one call writes the card from the
   table and concept notes, the glossary, the families and the
   conventions found in the programs. It is a proposal in
   `.build/card.proposed.md` until `--apply` (or `KNOWLEDGE_CARD_AUTO=1`)
   puts it into the bundle; a person's `knowledge/CARD.md` always wins;
6. the bundle's lexical parts are rebuilt and the report goes to
   `.build/consolidate.json`.

`--no-llm` runs the deterministic steps only. The model is
`KNOWLEDGE_CONSOLIDATE_MODEL` (default: the review model).

## The archive

Everything a session produced is kept twice: as the raw JSONL stream in
`logs/sessions/<day>/<session>.jsonl`, and normalised into **episodes**
in one SQLite file, `archive/curie.db` (`ARCHIVE_DB`). An episode is one
thread of one session: the business question, the follow-ups that
refined it, every model call with what was in its prompt (note ids,
exemplar ids, model, settings, token counts), every tool call with its
full arguments and result, every program version with its verdicts and
run, the feedback, the outcome, and what the user did next.

| Table | One row per |
|---|---|
| `sessions` | session log (identity, open/close, reason) |
| `episodes` | thread: `question`, `refinements`, `language`, `outcome`, `next_action`, `next_gap`, counters |
| `calls` | model call: `model`, `notes`, `exemplars`, `knowledge_chars`, `finish`, `tokens_in`, `tokens_out`, `text` |
| `tool_calls` | tool call: `name`, `args`, `ok`, `ms`, `result` (JSON, capped by `SESSION_LOG_RESULT_CHARS`), `tables` |
| `programs` | program version: `source`, `static`, `syntax`, `audit`, `confirmed`, `run`, `run_ok`, `exemplar_id`, `tables` |
| `events` | raw log record of the episode |
| `facts` | typed statement with evidence (filled by the consolidation pipelines, plan phase K4) |

Outcomes: `confirmed_ran_ok`, `ran_failed`, `discarded`, `abandoned_code`,
`no_program`, `denied`, `interrupted`, `answered`, `command`, `empty`.

The archive is written when a session closes (the log is imported in a
worker thread) and by `curie --archive import`, which back-fills every
log under `logs/sessions/`, including the ones written before the
richer logging of 11 September (they lack model names, token counts and
note ids, and their tool results are summaries only). Import is
idempotent: a log is re-read only when it changed. `curie --archive
stats` shows the counts; `curie --archive purge --last 15m` removes the
episodes of a test window (the logs stay). Nothing in the archive is
read on a user's turn; it is the input of the background pipelines
described in `KNOWLEDGE_PLAN.md`.

Since 11 September the logs carry, per model call, an `llm_call` record
(model, temperature, max_tokens, tool mode, the note and exemplar ids in
the prompt, the size of the knowledge block and the history) and an
`llm_done` record (finish reason, duration, token usage when the server
reports it through `stream_options`); `user_message` carries the thread
id and whether the message was a follow-up; `tool` records carry the
full result.

## The evaluation loop

Every change to prompts, notes, retrieval or the bundle can be measured
instead of judged: `curie --eval [--limit N] [--cases id,id]` takes the
archive's confirmed episodes (the question and the program the user
accepted), sends each question through the real turn (model, tools,
static rules, syntax check, audit) in a dev session, runs nothing
(`ABAP_REQUIRE_CONFIRM` is forced on), and measures per case whether a
family replayed it, how many program versions it took, whether the
first one passed the syntax check, whether the final one is runnable,
the audit verdict, whether its shape (tables, filters, aggregates,
joins) and its tables equal the accepted program's, whether the audit
verdict came from the memo, how many deterministic repairs were applied,
and the model and tool calls, tool errors and seconds. The report goes to
`knowledge/EVAL.md` with the previous run's numbers beside the new ones
(the summaries are kept in the archive's `eval_runs`); the sessions'
logs go to `logs/sessions/eval/` and never become episodes; the report's
Log column names each case's session, and `curie --trace <Log>` prints
its timeline (chapter 11). Nothing an
evaluation does reaches the notes: no program runs, so no exemplar,
insight or counter changes. A thread whose opening question was answered
without asking SAP (no tool call and no program before the first
follow-up, typically a how-to question that only later turned to data)
is excluded from the bank by default and named in the report, and so is
a case whose accepted program filters on identifiers or dates (four
digits or more) that the question does not carry, values the model took
from an earlier thread; `--include-general` replays them.

## Purging what a test session left behind

`curie --purge-knowledge --last 15m` lists what the window left behind
and, after one question (`--yes` skips it for scripts), moves it to
`knowledge/.trash/<timestamp>/` with the folders kept: the notes of
every type written in the window (tables, concepts, families, exemplars,
hints, insights; `--types` narrows the set), the reports `EVAL*.md` and
`REVIEW.md` written in the window (`--no-reports` keeps them), and it
removes the archived episodes of the window, the chat history in
`archive/curie.db` (`--no-archive` keeps them). `--all` takes every note
and report, moves the archive file and the compiled bundle `.build` to
the trash as well, asks twice, the second time for `PURGE <count>` typed
in full, and never accepts `--yes`: this is the clean slate before a new
SAP system. `CARD.md`, `README.md` and the session logs are never
touched. Nothing is deleted: move a folder or the archive file back out
of `.trash` to undo, or empty `.trash` when sure. The store ignores
dot-folders, so trashed notes are neither retrieved nor reviewed.

## Trust and limits

- Notes are read by the model as facts. Whoever can write to the folder
  can steer the assistant; protect it like configuration, keep it under
  the installation's own version control or backup, and review hints
  from time to time (the vault in Obsidian, the review report).
- Hints are labelled with their author and flagged as possibly imprecise;
  the prompt tells the model to verify them against the dictionary.
- The function-module policy, the table deny list and the static rules are
  code and `.env`, never notes.
- Retrieval is by names and word stems, so a note nobody mentions by table
  name, alias or keyword is not found; give notes good aliases. Embedding
  retrieval over long documents is a possible later stage, not part of this
  one.
- Exemplars are examples: the model still writes the program. Catalogued
  reports are templates, replayed without the model, but every gate (rules,
  syntax check, audit, confirmation) still applies to them.
- An answer cut at `LLM_MAX_OUTPUT_TOKENS` is asked for again once with
  twice the budget (`LLM_GROW_ON_TRUNCATION`); reasoning models spend the
  budget on hidden thinking first.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `KNOWLEDGE_ENABLED` | `1` | Switch the whole layer off with `0` |
| `KNOWLEDGE_DIR` | `knowledge` | The folder, relative to `src/curie/cli.py` |
| `KNOWLEDGE_MAX_NOTES` | `8` | Notes per prompt |
| `KNOWLEDGE_MAX_CHARS` | `6000` | Their total size |
| `KNOWLEDGE_EXEMPLARS` | `2` | Confirmed programs per prompt |
| `KNOWLEDGE_CAPTURE_EXEMPLARS` | `1` | Keep confirmed programs that ran |
| `KNOWLEDGE_CAPTURE_INSIGHTS` | `1` | Distil fixed failures into pitfalls (one model call per such run) |
| `KNOWLEDGE_MAX_INSIGHTS` | `4` | Pitfalls per prompt |
| `KNOWLEDGE_CATALOGUE` | `1` | Replay catalogued reports without the model |
| `KNOWLEDGE_ROUTER`, `KNOWLEDGE_ROUTER_MODEL` | `1`, audit model | The model picks a family when none matches lexically but some share a table or words with the question |
| `KNOWLEDGE_REVIEW_INTERVAL` | `0` | Seconds between in-process review runs; 0 = on demand only |
| `KNOWLEDGE_REVIEW_APPLY` | `0` | The job also applies safe fixes |
| `KNOWLEDGE_REVIEW_MAX` | `50` | Notes per run |
| `KNOWLEDGE_REVIEW_MODEL` | `LLM_AUDIT_MODEL` | Model for the verdicts |
