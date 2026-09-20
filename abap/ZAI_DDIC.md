# Curie – DDIC objects (SE11)

All objects are **structures** (SE11 → Data type → Structure on 4.5B,
"Structure" radio button on ECC). No transparent tables are needed.
Create them in package `ZAIC` (development class on 4.5B), the package
that holds every Curie object, so one transport carries the installation;
`$TMP` only for a throw-away test. Activate
each structure before creating the function group.

Field typing: on 4.5B every structure component needs a **data element**
(the "built-in type" toggle of later releases is not available). The
standard data elements `CHAR1`, `CHAR8`, `CHAR16`, `CHAR32`, `CHAR40`,
`CHAR72`, `CHAR80`, `CHAR255` and `INT4` exist on 4.5B and ECC (check with
SE11 → Data element → Display). If one is missing on your system, create
`ZAI_CHARnn` with domain `CHARnn` (or a new domain, data type CHAR,
length nn) and use it instead — Curie reads the real field lengths from
the function module interface at startup and adapts.

## ZAI_LINE – one source line (T_SOURCE, T_PROMPT)

| Field | Data element | Type | Length | Note |
|---|---|---|---|---|
| LINE | CHAR72 | CHAR | 72 | 4.5B editor width. On ECC 6 `CHAR255` may be used; Curie wraps the generated source to whatever length it finds in the interface. |

## ZAI_LISTLINE – one line of the ABAP list (T_LIST)

| Field | Data element | Type | Length | Note |
|---|---|---|---|---|
| LINE | CHAR255 | CHAR | 255 | Matches `LISTZEILE` used by `LIST_TO_ASCI`. |

## ZAI_PARAMS – reference fields for the function module interfaces

The structure is never filled with data; it only provides typed reference
fields so that SE37 on 4.5B accepts `LIKE ZAI_PARAMS-<field>` for every
scalar parameter of a remote-enabled function module.

| Field | Data element | Type | Length | Used by |
|---|---|---|---|---|
| SESSION_ID | CHAR32 | CHAR | 32 | I_SESSION_ID, E_SESSION_ID |
| RUN_ID | CHAR8 | CHAR | 8 | I_RUN_ID |
| STATE | CHAR16 | CHAR | 16 | E_STATE |
| STATUS_TEXT | CHAR80 | CHAR | 80 | E_STATUS_TEXT |
| URL | CHAR255 | CHAR | 255 | E_URL |
| PROMPT | CHAR255 | CHAR | 255 | I_PROMPT |
| REASON | CHAR32 | CHAR | 32 | I_REASON |
| MESSAGE | CHAR255 | CHAR | 255 | I_MESSAGE, E_MESSAGE |
| PROGNAME | CHAR40 | CHAR | 40 | (alternative to `LIKE SY-REPID`) |
| MODE | CHAR1 | CHAR | 1 | I_MODE |
| SYNTAX_OK | CHAR1 | CHAR | 1 | E_SYNTAX_OK |
| WORD | CHAR72 | CHAR | 72 | E_WORD |
| HEARTBEAT_SEC | INT4 | INT4 | 10 | E_HEARTBEAT_SEC (if `TYPE I` is refused) |
| RC | INT4 | INT4 | 10 | I_RC (if `TYPE I` is refused) |
| LINE_NO | INT4 | INT4 | 10 | E_LINE (if `TYPE I` is refused) |

Integer parameters should be declared `TYPE I` in SE37. Should the 4.5B
SE37 refuse a built-in type for a remote-enabled module, use
`LIKE ZAI_PARAMS-HEARTBEAT_SEC` / `-RC` / `-LINE_NO` (or `LIKE SY-INDEX`)
instead; both arrive as `INT` on the RFC side.

## Checklist

1. SE11: create and activate `ZAI_LINE`, `ZAI_LISTLINE`, `ZAI_PARAMS`.
2. Verify with SE11 → Display: every component shows a length and no
   component is flagged as inactive.
3. Only then create function group `ZAI` (see `ZAI_FUNCTION_GROUP.md`);
   its interfaces reference these structures.
