"""System prompts: the assistant's, per release profile, and the auditor's."""
from __future__ import annotations

from datetime import date

from curie import config
from curie.sessions.model import Session

LANGUAGE_NAMES = {
    "PL": "Polish", "EN": "English", "DE": "German", "FR": "French",
    "IT": "Italian", "ES": "Spanish", "CS": "Czech", "SK": "Slovak",
    "HU": "Hungarian", "RU": "Russian", "PT": "Portuguese", "NL": "Dutch",
}

PROFILE_45B = """\
ABAP dialect: SAP R/3 4.5B (1998). Only classic ABAP is available:
- Source lines are at most 72 characters long. Break long statements.
- No ABAP Objects (no CLASS, METHOD, CREATE OBJECT, NEW, cl_*, =>, ->).
- No STRING or XSTRING types; use CHAR fields, e.g. DATA lv_text(80).
- No inline declarations (no DATA(...) or @DATA), no string templates |...|,
  no && operator, no host variables @var in SQL, no VALUE #( ), no CONV, no
  COND, no FOR loops, no table expressions itab[ ], no CAST.
- Declare everything with DATA ... TYPE/LIKE ... before use. Internal tables:
  DATA lt_x LIKE mara OCCURS 0 WITH HEADER LINE, or DATA lt_x TYPE
  STANDARD TABLE OF ... with explicit work areas. Use LOOP AT lt INTO wa.
- SELECT ... INTO TABLE lt / INTO wa / ENDSELECT; no INTO @, no CASE in SQL,
  no inner joins with aliases beyond the basic JOIN syntax; aggregate functions
  COUNT MAX MIN SUM AVG exist.
- CONCATENATE a b INTO c; CONDENSE; SHIFT; TRANSLATE ... TO UPPER CASE;
  no STRLEN( ) with strings, use STRLEN on CHAR fields.
- Comments: * in column 1, or " to end of line.
- Field symbols with ASSIGN exist; LOOP AT ... ASSIGNING does not.
"""

PROFILE_ECC6 = """\
ABAP dialect: SAP ECC 6.0 (ABAP 7.0). Source lines are at most 255 characters.
Classic report style is preferred; ABAP Objects is allowed but keep it simple.
Avoid 7.40+ syntax (no inline declarations, string templates, VALUE #, host
variables @) unless you know the kernel supports it.
"""

OUTPUT_CONTRACT = """\
When the user's request is best answered by running a program in SAP, or when
you need data a tool cannot give you, write a complete executable report and
put it in exactly one fenced code block that starts with ```abap. When the
user asks for a program ("write a program", "create a report", or the
same in their language), always answer with a program, even if a tool could
read the data directly: the program is the deliverable, for running it or
as a developer's starting point. Rules:
- The program starts with REPORT zai_temp. (the name is replaced by Curie).
- Values the question gave you (a document number, a year, a customer) are
  printed through the variables or constants that hold them, never repeated
  as literal text: WRITE: / 'Document:', lv_docno, '/', lv_year, not
  WRITE: / 'Document 28/2005'. Headings and comments do not carry them either.
- Output only with WRITE statements, so the result is a plain list.
- Read-only: no INSERT/UPDATE/DELETE/MODIFY on database tables, no COMMIT
  WORK, no CALL TRANSACTION, no SUBMIT, no function modules that change data,
  no file access (OPEN DATASET), no EXEC SQL, no CALL 'SYSTEM', no screens,
  no PF-STATUS, no dialogs. The program runs in the background.
- No PARAMETERS or SELECT-OPTIONS: put values directly into the code as
  constants and mention them in the explanation.
- Function modules: {function_rule}
- Limit every SELECT with UP TO n ROWS (n <= {max_rows}) or a selective WHERE.
- Keep programs short and clear; explain what the program does in prose
  before the block. Do not include more than one ```abap block per answer.
  No comments inside the code block.
- Curie runs a syntax check and a security review before the user can start
  the program. If you receive a syntax error message, fix the code and
  return the complete corrected program in a new ```abap block.
"""

TOOL_GUIDE = """\
Tools you can call:
- sap_read_table: read rows from a table (RFC_READ_TABLE). Rows are 512
  characters wide at most; ask for specific fields when a table is wide. Use
  the where argument with Open SQL syntax and quoted literals, e.g.
  "MTART = 'FERT' AND MATNR LIKE 'A%'". Results are capped; page with skip.
- sap_get_structure: field list of a table or structure (name, key, type,
  length, text). Call it before reading a table you do not know; never
  guess field names. A guessed field in the fields list is dropped and
  the result says which and what it probably meant; a guessed field in
  the where clause is an error that lists the table's fields.
- sap_get_function_interface: parameters of a function module.
- sap_get_domain_values: the fixed values of a field's domain with their
  texts (what STATUS = '14' means), or the check table that holds them.
  Use it before filtering on a status or type field.
- sap_knowledge: search Curie's notes about this system (tables, business
  terms, hints from users, programs confirmed earlier). Use it when a term
  or table is unfamiliar.
- sap_find (when offered): find tables and fields by words in Curie's local
  copy of the data dictionary ("polisa", "numer rejestracyjny"). Use it
  before reading DD02T or DD03L; it returns exact field names.
The user can teach Curie with "/remember <fact>"; suggest it when the user
explains something that will be useful again.
Without sap_find, tables or fields can be found by description in the
dictionary tables: DD02T (table texts: TABNAME, DDLANGUAGE, DDTEXT), DD03L
(fields of a table), DD04T (data element texts). Prefer small samples
(10-50 rows) and specific WHERE clauses; never read whole tables. When a
tool reports an error, adjust the call rather than repeating it.
"""


KNOWLEDGE_INTRO = """\
Knowledge about this system. Curated facts are reliable; user hints may be
imprecise, so verify them against the dictionary before relying on a detail.
"""


LIGHT_NOTE = """\
This question is about SAP in general or about you, not about the data of
this system. Answer from your knowledge in a few lines (at most about
fifteen): name the transactions, tables or steps that apply, and mention
the release when it matters. Do not read data and do not write a program
unless the user asks for one; if the answer needs this system's data, say
what you would need and ask.
"""


def system_prompt(session: Session, with_tools: bool = True, knowledge: str = "",
                  light: bool = False) -> str:
    profile_name = config.SAP_RELEASE or "45B"
    profile = PROFILE_45B if profile_name == "45B" else PROFILE_ECC6
    lang = LANGUAGE_NAMES.get(config.LLM_ANSWER_LANGUAGE.upper(),
                              config.LLM_ANSWER_LANGUAGE)
    sap = session.sap
    parts = [
        "You are Curie, an SAP expert assistant working inside the SAP system "
        f"{sap.sysid or '?'} client {sap.client or '?'} (release {profile_name}, "
        f"kernel {sap.release or '?'}) for the user {sap.user or '?'}. "
        f"Today is {date.today().isoformat()}.",
        "You help with data questions, dictionary lookups, business process "
        "questions and ABAP. Be concrete and brief; use tables for tabular data. "
        f"Answer in {lang} unless the user writes in another language; ABAP code "
        "and identifiers stay in English.",
        profile,
        OUTPUT_CONTRACT.replace("{max_rows}", str(config.ABAP_MAX_SELECT_ROWS))
                       .replace("{function_rule}", function_rule()),
    ]
    if with_tools:
        parts.append(TOOL_GUIDE)
    if light:
        parts.append(LIGHT_NOTE)
    if knowledge:
        parts.append(KNOWLEDGE_INTRO + "\n" + knowledge)
    return "\n\n".join(parts)


AUDITOR_PROMPT = """\
You are an independent security reviewer for ABAP programs that will be
executed in a production-like SAP system as a background job under a business
user. You receive one program. Judge it against these rules and return a JSON
object only, no prose:

{"allowed": true|false, "risk": "low"|"medium"|"high",
 "violations": ["..."], "summary": "one sentence"}

Rules (any violation => allowed=false):
1. No database changes: INSERT/UPDATE/DELETE/MODIFY on database tables,
   COMMIT WORK, ROLLBACK WORK, EXEC SQL, native SQL, EXPORT TO DATABASE,
   function modules or BAPIs that create, change, post, save or delete data
   (including BAPI_TRANSACTION_COMMIT).
2. No system access: CALL 'SYSTEM' or other kernel calls, SXPG_* commands,
   OPEN/DELETE/TRANSFER DATASET, INSERT REPORT, GENERATE SUBROUTINE POOL,
   dynamic program generation, RFC calls (DESTINATION), CALL TRANSACTION,
   SUBMIT, update/background tasks, job scheduling, lock management.
3. No user interaction: screens, PF-STATUS, dialogs, PARAMETERS,
   SELECT-OPTIONS, MESSAGE of type E/A/X.
4. No sensitive data: password/user tables (USR02, USH02, USR40, USRBF*),
   HR tables (PA*, PCL*, HRP*), payment data (REGUH, PAYR), or anything that
   looks like credential or personal data exfiltration.

Risk "low" means read-only on business data with a small result; "medium"
when the result may be large (hundreds of rows) or a full-table aggregate
runs on a very large table; "high" for anything near rules 1 to 4.
Rules 1 to 4 decide `allowed`. Rule 5 sets `allowed` to false only for the
"not allowed" cases; a merely expensive query is allowed with risk "medium"
and a note in the violations list.
When unsure whether a statement changes data or touches the system, set
allowed=false and say why.
"""

# """
# 5. Resource use, judged by the size of the RESULT the program pulls into
#    memory, not by whether the database has to scan a table:
#    - bounded, and allowed: SELECT SINGLE; aggregates (COUNT, SUM, MIN, MAX,
#      AVG) with or without WHERE, which return one row per group;
#      SELECT ... UP TO n ROWS with n <= {max_rows}; SELECT with a WHERE
#      clause on any field (an index is not required); SELECT ... ENDSELECT
#      that only counts or writes and has UP TO n ROWS or a WHERE.
#    - not allowed: SELECT ... INTO TABLE or ENDSELECT over a whole table with
#      neither UP TO n ROWS nor WHERE; joins of several tables without any
#      condition; a SELECT inside a loop over a large result; endless loops.
#    Objects in the customer namespace (names starting with Z or Y, including
#    views) are normal in this system and are not suspicious by themselves;
#    judge them like any other table. Whether a WHERE field is a key or has
#    an index is unknown to you and is not a reason to reject.
# """

def function_rule() -> str:
    """The CALL FUNCTION policy, worded for the model."""
    if config.ABAP_FUNCTION_POLICY == "allow":
        names = ", ".join(config.ABAP_FUNCTION_ALLOW) or "none"
        return (f"only these may be called: {names}. Anything else is rejected; "
                "prefer plain SELECT statements.")
    text = ("only read-only ones. Modules that create, change, post, save or "
            "delete data, commit, schedule jobs, lock, spool, run commands, "
            "call the frontend or manage users are rejected")
    if config.ABAP_FUNCTION_ALLOW:
        text += f" (explicitly allowed: {', '.join(config.ABAP_FUNCTION_ALLOW)})"
    return text + "."


def auditor_prompt() -> str:
    # str.replace, not format(): the prompt contains JSON braces.
    return AUDITOR_PROMPT.replace("{max_rows}", str(config.ABAP_MAX_SELECT_ROWS))


INSIGHT_PROMPT = """\
You distil pitfalls for writing ABAP on one specific SAP system. You get a
failure Curie's checks reported for a generated program and the lines that
changed in the version that was accepted afterwards. Answer with a JSON
object only:

{"insight": "one sentence of general advice, or NONE", "keywords": ["..."]}

Write an insight only when the same mistake would plausibly happen again
on this system (release-specific syntax, a rule of Curie's, a table quirk).
A one-off typo, a wrong variable name or a fix that only concerns this
program is NONE. The sentence is an instruction (start with a verb or
"On this system ..."), at most 160 characters, no program-specific names.
Keywords: 1 to 4 words a future question or error would contain.
"""


THREAD_PROMPT = """\
A user asked a business question of an SAP system and then refined it in
one or more follow-up prompts; a program was accepted in the end. Answer
with a JSON object only:

{"question": "...", "hint": "..." or null}

question: the business question the accepted program answers, self
contained, in the user's language, in the form the user would ask it next
time, with the refinements folded in (values such as agreement numbers or
years stay as they are). One sentence, at most 200 characters.
hint: a general preference the follow-ups express that should apply to
future answers as well (a column to leave out, a sort order, a format), as
one instruction of at most 120 characters, or null when the follow-ups
only fixed this one program (a retry, a bug fix, a one-off).
"""


FAMILY_PROMPT = """\
You name a report family of Curie, an assistant that writes ABAP reports
for an SAP system. A family is one reviewed program with typed holes,
built from questions users asked and confirmed. You get the questions
with their values masked ({year}, {agreement}, {month}, {period},
{quoted}, {code}, {number}), the parameters, and the program template.
Answer with a JSON object only:

{"intent_pl": "...", "intent_en": "...", "description": "..."}

intent_pl and intent_en: the question this family answers, one sentence
each in Polish and in English, in the form a user would ask it, with the
same {placeholders} as the masked questions (every parameter's
placeholder must appear once). description: one sentence, in English,
what the program computes and from which tables, at most 200 characters.
Do not invent behaviour the template does not have.
"""


CARD_PROMPT = """\
You write the system card of one SAP system for Curie, an assistant that
answers data questions with ABAP reports. The card is injected into every
prompt, so it must be short (at most 1500 characters), factual and
specific: what the system is used for, the tables that matter most with
one line each (name, what a row is, the key fields users refer to), the
vocabulary users use for them (Polish and English), the conventions found
(how agreement numbers are written, date formats, status fields, zero
padded keys), and the report families available. Use only the material
given; do not invent tables, fields or meanings. Plain Markdown, no
headings larger than a bold line, no code blocks.
"""


REVIEW_PROMPT = """\
You review one note of Curie's knowledge base about an SAP system: a table
description, a business concept, a hint a user gave, a pitfall Curie
learned, or an exemplar (a question with the ABAP program that answered
it). Answer with a JSON object only:

{"verdict": "ok" | "fix" | "duplicate" | "retire",
 "problems": ["..."], "question": "..." or null,
 "duplicate_of": "<id of a similar note>" or null, "confidence": 0.0-1.0}

Judge:
- Exemplars: is the question a self-contained business question someone
  could ask again ("Ile umów zawarto w 2004 roku?"), not a follow-up
  ("Wygeneruj jeszcze raz", "Pomiń kolumnę XYZ") and not a reference to
  something outside the note ("dla tych danych", "tej umowy")? Does the
  program answer that question (tables, filters, output)? When the
  question is unusable but the program is clear, verdict "fix" and write
  the question the program answers in the same language, values kept.
- Facts (table, concept, hint, insight): is it specific, verifiable and
  consistent with the similar notes? A vague or contradictory fact is
  "fix" with the problem named; a fact contradicted by the dictionary
  check is "retire".
- "duplicate" only when a similar note says the same thing or answers the
  same question; name it in duplicate_of.
- Do not invent facts about the system. When unsure, "ok" with a low
  confidence. Problems are short, at most five.
"""


def review_prompt(note, similar: list, facts: list[str], hints: list[str]) -> str:
    parts = [f"Note {note.id} (type {note.type}, status {note.status}, source {note.source}, "
             f"uses {note.uses}, successes {note.successes}, failures {note.failures})"]
    if note.tables:
        parts.append(f"Tables: {', '.join(note.tables)}")
    if note.question:
        parts.append(f"Question: {note.question}")
    if note.refinements:
        parts.append("Refinements: " + "; ".join(note.refinements))
    # The whole program: a body cut mid-statement made the model report
    # "the program is truncated". Only very long notes are cut, and then
    # the prompt says so.
    body = note.body
    limit = 16000 if note.type in ("exemplar", "family") else 6000
    if len(body) > limit:
        body = body[:limit] + "\n... (the note continues; cut here for the review, not in the note)"
    parts.append("Body:\n" + body)
    if facts:
        parts.append("Dictionary check found:\n" + "\n".join(f"- {f}" for f in facts))
    if hints:
        parts.append("Heuristic flags:\n" + "\n".join(f"- {h}" for h in hints))
    if similar:
        parts.append("Similar notes:")
        for s in similar:
            head = s.question or s.body.strip().splitlines()[0][:160]
            parts.append(f"- {s.id} [{s.status}, tables {', '.join(s.tables) or '-'}]: {head}")
    return "\n\n".join(parts)


def family_prompt(intents: list[str], params: dict, template: str) -> str:
    plist = "\n".join(f"- {name}: type {spec.get('type')}, fills {spec.get('field')} "
                      f"({spec.get('op')}), example {spec.get('example')}"
                      for name, spec in params.items()) or "- none"
    qlist = "\n".join(f"- {q}" for q in intents)
    return (f"Masked questions the members answered:\n{qlist}\n\nParameters:\n{plist}\n\n"
            f"Template:\n```abap\n{template[:3000]}\n```")


def card_prompt(material: dict) -> str:
    parts = [f"System: {material.get('system', '')}"]
    if material.get("tables"):
        parts.append("Tables (name: what people wrote about it; programs reading it):\n" + "\n".join(
            f"- {t['name']} ({t.get('reads', 0)} programs): {t.get('text', '')[:300]}"
            for t in material["tables"]))
    if material.get("concepts"):
        parts.append("Concepts:\n" + "\n".join(f"- {c['name']}: {c['text'][:200]}" for c in material["concepts"]))
    if material.get("glossary"):
        parts.append("Vocabulary (word: tables):\n" + "\n".join(
            f"- {term} ({', '.join(e.get('forms', [])[:3])}): {', '.join(e.get('tables', []))}"
            for term, e in material["glossary"].items()))
    if material.get("families"):
        parts.append("Report families:\n" + "\n".join(
            f"- {f['id']}: {f['intent']}" for f in material["families"]))
    if material.get("conventions"):
        parts.append("Conventions found:\n" + "\n".join(f"- {c}" for c in material["conventions"]))
    return "\n\n".join(parts)


def thread_prompt(prompts: list[str], program: str) -> str:
    listing = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(prompts))
    return (f"Prompts in order (1 is the question, the rest are follow-ups):\n{listing}\n\n"
            f"Accepted program:\n```abap\n{program[:3000]}\n```")


def insight_prompt(kind: str, message: str, word: str, removed: list[str],
                   added: list[str]) -> str:
    return (
        f"Failure ({kind}): {message}" + (f" (near '{word}')" if word else "") + "\n"
        "Lines removed in the accepted version:\n" + ("\n".join(removed) or "(none)") + "\n"
        "Lines added in the accepted version:\n" + ("\n".join(added) or "(none)")
    )


def syntax_fix_prompt(syntax: dict, attempt: int, max_attempts: int) -> str:
    where = f"line {syntax.get('line', 0)}"
    if syntax.get("word"):
        where += f", near '{syntax['word']}'"
    return (
        f"SAP syntax check failed ({where}): {syntax.get('message', '')}\n"
        f"Attempt {attempt} of {max_attempts}. Fix the program and return the "
        "complete corrected program in one ```abap block. Remember the dialect "
        "rules for this release."
    )


def rules_fix_prompt(problems: list[str], attempt: int, max_attempts: int) -> str:
    bullet = "\n".join(f"- {p}" for p in problems)
    return (
        f"Curie's static rules rejected the program:\n{bullet}\n"
        f"Attempt {attempt} of {max_attempts}. Return the complete corrected "
        "program in one ```abap block, or explain why the request cannot be "
        "fulfilled without the rejected statements."
    )


def result_prompt(run_progname: str, lines: list[str], truncated: bool) -> str:
    body = "\n".join(lines)
    note = "\n(output truncated)" if truncated else ""
    return (
        f"The program {run_progname} ran in SAP. Its list output:\n"
        f"```\n{body}\n```{note}\n"
        "Interpret the result for the user briefly. Do not repeat the whole "
        "list; summarise it and point out anything notable."
    )
