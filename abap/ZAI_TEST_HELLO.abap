REPORT zai_test_hello.
*----------------------------------------------------------------------*
* Curie: minimal 4.5B-valid sample program for testing the execution
* path by hand. Paste the whole text into the Curie chat inside an
* ```abap fence, or feed it to Z_AI_PROG_CHECK_AND_EXECUTE in SE37
* (mode 'S') as T_SOURCE with I_PROGNAME = ZAI_TEST_HELLO.
* Curie replaces the REPORT line with its own program name.
*----------------------------------------------------------------------*

DATA: lt_t000 LIKE t000 OCCURS 0 WITH HEADER LINE.

WRITE: / 'Hello from Curie'.

SELECT * FROM t000 INTO TABLE lt_t000 UP TO 5 ROWS.

LOOP AT lt_t000.
  WRITE: / lt_t000-mandt, lt_t000-mtext.
ENDLOOP.

IF sy-subrc <> 0.
  WRITE: / 'No clients found in T000'.
ENDIF.
