# 08 ABAP execution

The code path in [`llm/abap_flow.py`](../src/curie/llm/abap_flow.py), from a model
answer to a list in the browser.

```
answer ──► extract ```abap ──► prepare (REPORT, fold to 72/255, codepage)
   ──► repairs (period before ENDIF, `*` column, UP TO before WHERE) ──► dialect check
   ──► static rules ──fail──► fix round trip (≤ ABAP_FIX_RETRIES)
   ──► syntax check in SAP (mode C) ──fail──► fix round trip
   ──► model audit (or the memo's verdict) ──► code card [Run in SAP] [Discard]
   ──► confirm ──► mode X (job) or S (sync) ──► list ──► model explains it
```

## 1. Extract and prepare

- The first ```` ```abap ```` block of the answer (case-insensitive fence). A
  second block produces a warning and is ignored.
- A leading `REPORT`/`PROGRAM` statement is removed and
  `REPORT zai_<8 hex>.` injected; the name is `ABAP_PROG_PREFIX` + 8 hex.
- Lines are folded to the effective width: the smaller of the profile width
  (72 on 4.5B) and the `T_SOURCE` line length read from the backend. Folds
  happen at spaces outside string literals; comment lines (`*`) and
  trailing `"` comments are folded as comments; a line that cannot be folded
  (a literal longer than the width) is reported as a problem.
- Codepage: typographic dashes, quotes, ellipsis, arrows and non-breaking
  spaces become ASCII; anything still outside the backend codepage
  (ISO-8859-2 on the 4.5B profile) becomes `?`. Changes are listed in an
  info notice; they never block.
- Repairs (`ABAP_REPAIR`, [`sap_tools/abap_source.py`](../src/curie/sap_tools/abap_source.py)
  `repair_source`): the three syntax errors the archive showed again and
  again, each with the same one-token fix, are repaired before SAP is
  asked: a period missing before `ELSE`, `ENDIF`, `ENDLOOP` and the other
  block keywords (added to the previous statement unless it visibly
  continues with an operator; a trailing comma becomes the period); a
  `*` comment line the model indented (moved to column 1 when no
  statement is open, so a `*` continuing an arithmetic expression
  stays); `UP TO n ROWS` after `WHERE`, `GROUP BY` or `ORDER BY` (moved
  in front of `WHERE`, where every release accepts it); `DESC` and `ASC`
  in `ORDER BY` or `SORT ... BY`, which ABAP spells `DESCENDING` and
  `ASCENDING` (SAP reports an unknown column "DESC"). Each repair is
  listed in an info notice ("Repaired before the check: line 4: period
  added before ENDIF"), in the `code_checked` log record and on the run.
- Dialect check (`dialect_problems`, 4.5B profile only): inline
  `DATA(...)`, `@` host variables, `VALUE #(` and the other constructor
  expressions, string templates, `->`/`=>` and table functions are
  reported as rule problems with the classic alternative named, so the
  model gets the reason instead of a bare `Field "DATA" is unknown`.

## 2. Static rules

[`llm/audit.py`](../src/curie/llm/audit.py) works on statements (split at periods
outside literals, comments removed, literals blanked). Violations carry
line numbers.

Forbidden statements: `COMMIT WORK`, `ROLLBACK WORK`, `EXEC SQL`,
kernel calls `CALL '...'`, `SYSTEM-CALL`, `INSERT REPORT`, `DELETE REPORT`,
`GENERATE SUBROUTINE POOL/REPORT/DYNPRO`, `CALL TRANSACTION`,
`LEAVE TO TRANSACTION`, `SUBMIT`, `OPEN/DELETE/TRANSFER/TRUNCATE DATASET`,
`CALL FUNCTION ... DESTINATION`, `STARTING NEW TASK`, `IN UPDATE TASK`,
`IN BACKGROUND TASK`, `SET PF-STATUS`, `CALL SCREEN/DIALOG/SELECTION-SCREEN`,
`EDITOR-CALL`, `CALL METHOD OF` (OLE), `EXPORT ... TO DATABASE/SHARED ...`,
`DELETE FROM DATABASE`, `PARAMETERS`, `SELECT-OPTIONS`, `SELECTION-SCREEN`,
`MESSAGE` of type E/A/X.

Database changes: `UPDATE`, `DELETE FROM dbtab`, `INSERT INTO dbtab`,
`INSERT dbtab FROM`, `... CLIENT SPECIFIED`; `MODIFY x FROM` and
`DELETE x WHERE` when `x` is not an internal table declared in the program
(`OCCURS`, `TYPE/LIKE ... TABLE OF`, `RANGES`, `TYPE RANGE OF`).

Function modules: every `CALL FUNCTION 'NAME'` is checked against the
policy in `.env` (`ABAP_FUNCTION_POLICY`, `ABAP_FUNCTION_DENY`,
`ABAP_FUNCTION_ALLOW`; the allow list wins). Dynamic names in variables are
not caught by this rule; the model audit is the second line.

Reads: `SELECT` (other than `SINGLE` or an aggregate) must have
`UP TO n ROWS` with n ≤ `ABAP_MAX_SELECT_ROWS` or a `WHERE`; tables in
`READ_TABLE_DENY` may not appear after `FROM` or `JOIN`.

A violation posts a code card marked "rejected by static rules" and sends
the problems to the model as an automatic message; the corrected answer goes
through the path again, up to `ABAP_FIX_RETRIES` times.

## 3. Syntax check

`Z_AI_PROG_CHECK_AND_EXECUTE` mode `C`. A syntax error is posted as a
`syntax_error` event (line, word, message, attempt) and fed back to the
model the same way. Verified on 4.5B: an unknown field comes back as
`Field "LV_UNDEFINED" is unknown ...`, line 8, word `LV_UNDEFINED`.

If the function module itself fails (a dump, a missing include, a lost
connection), the result is flagged as an infrastructure problem: the card
says "not checked in SAP", an error notice explains, and the model is not
asked to change anything.

## 4. Audit

The model review (see 07). The card shows the verdict, risk and violations.
With `ABAP_AUDIT_BLOCKS=1` a negative verdict disables the Run button.
A program whose shape equals one the reviewer already allowed and the
user confirmed keeps that verdict (`ABAP_AUDIT_MEMO`, see 07): no second
model call, and the card's summary names the date and the run the
verdict came from. A replay from the catalogue with unchanged values
keeps the verdict stored with the exemplar, as before.

## 5. Confirm

State `CONFIRM`; the page shows Run and Discard. A new prompt discards
waiting code. `ABAP_REQUIRE_CONFIRM=0` would run allowed code immediately;
keep it at 1.

## 6. Execute

`ABAP_EXEC_MODE=job` (default): mode `X`. The function module checks again,
does `INSERT REPORT`, opens a job, submits `ZAI_RUNNER` with the program
name, session id and run id **as the end user** (`USER i_user`), and
releases it immediately. Curie emits `run_started` with the job name and
count and waits for `Z_AI_LIST_DELIVER` up to `ABAP_RUN_TIMEOUT`. Measured
on TC0: about 25 seconds from confirmation to list, most of it job
scheduling.

On timeout Curie reads `TBTCO`: status `A` means the job was cancelled (a
short dump in the program; see ST22), `F` means it finished without
delivering (check the destination from the job log), anything else means it
is still on its way and a late delivery will still be shown.

`ABAP_EXEC_MODE=sync`: mode `S`. The program runs inside the RFC call as
the RFC user and the list comes back in `T_LIST`; a dump arrives directly as
an RFC runtime failure with the dump key. Useful for development and for
systems without free background work processes.

## 7. Result

`run_result` carries the lines; the page shows them in a monospace block
with a copy button. With `ABAP_INTERPRET_RESULT=1` the first 300 lines (at
most 12,000 characters) go to the model as an automatic message and its
summary is streamed as a normal answer. The generated program is deleted
(mode `D`); a sweep at startup deletes leftover `ZAI_` + 8 hex programs
created before today and leaves `ZAI_RUNNER` and `ZAI_START` alone.

## Stop and discard

Stop during RUNNING aborts the job (`BP_JOB_ABORT` on ECC; on 4.5B
`BAPI_XMI_LOGON` version 1.0, `BAPI_XBP_JOB_ABORT`, `BAPI_XMI_LOGOFF`),
cancels the wait and deletes the program. Discard marks the run and tells
the model the user declined.
