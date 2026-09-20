REPORT zai_runner.
*----------------------------------------------------------------------*
* Curie: wrapper report, scheduled by Z_AI_PROG_CHECK_AND_EXECUTE
* (mode 'X') as a background job running under the END USER.
*
* 1. SUBMIT the generated ZAI_* program with LIST TO MEMORY
* 2. LIST_FROM_MEMORY + LIST_TO_ASCI -> plain text lines
* 3. push the lines to Curie: Z_AI_LIST_DELIVER DESTINATION 'SAP_AI'
*
* A short dump inside the generated program cancels this job before
* step 3; Curie detects that through its run timeout and TBTCO.
*----------------------------------------------------------------------*

PARAMETERS: p_prog     LIKE sy-repid,
            p_sess(32) TYPE c LOWER CASE,
            p_run(8)   TYPE c LOWER CASE.

DATA: lt_obj  LIKE abaplist     OCCURS 0 WITH HEADER LINE,
      lt_asci LIKE listzeile    OCCURS 0 WITH HEADER LINE,
      lt_list LIKE zai_listline OCCURS 0 WITH HEADER LINE,
      lv_rc          TYPE i,
      lv_msg(255)    TYPE c,
      lv_rfcmsg(80)  TYPE c.

CONSTANTS: c_dest(6) TYPE c VALUE 'SAP_AI'.

START-OF-SELECTION.

  CLEAR: lv_rc, lv_msg.

  IF p_prog IS INITIAL OR p_prog(4) <> 'ZAI_'.
    lv_rc  = 8.
    lv_msg = 'ZAI_RUNNER: program name must start with ZAI_'.
  ELSE.
*   Run the generated program; its list goes to ABAP memory.
    SUBMIT (p_prog) EXPORTING LIST TO MEMORY AND RETURN.

    CALL FUNCTION 'LIST_FROM_MEMORY'
         TABLES
              listobject = lt_obj
         EXCEPTIONS
              OTHERS     = 1.
    IF sy-subrc <> 0.
*     No list in memory: the program wrote nothing. Not an error.
      lv_rc  = 0.
      lv_msg = 'Program produced no list output'.
    ELSE.
      CALL FUNCTION 'LIST_TO_ASCI'
           EXPORTING
                list_index = -1
           TABLES
                listasci   = lt_asci
                listobject = lt_obj
           EXCEPTIONS
                OTHERS     = 1.
      IF sy-subrc <> 0.
        lv_rc  = 4.
        lv_msg = 'LIST_TO_ASCI failed'.
      ENDIF.
    ENDIF.
  ENDIF.

  LOOP AT lt_asci.
    lt_list-line = lt_asci.
    APPEND lt_list.
  ENDLOOP.

*---------------------------------------------------------------------*
* Deliver the list to Curie. An error here cancels the job, so the
* failure is visible in SM37 and to Curie's TBTCO check.
*---------------------------------------------------------------------*
  CALL FUNCTION 'Z_AI_LIST_DELIVER' DESTINATION c_dest
       EXPORTING
            i_session_id          = p_sess
            i_run_id              = p_run
            i_rc                  = lv_rc
            i_message             = lv_msg
       TABLES
            t_list                = lt_list
       EXCEPTIONS
            unknown_run           = 1
            communication_failure = 2 MESSAGE lv_rfcmsg
            system_failure        = 3 MESSAGE lv_rfcmsg
            OTHERS                = 4.
  IF sy-subrc <> 0.
    IF lv_rfcmsg IS INITIAL.
      lv_rfcmsg = 'unknown run or session'.
    ENDIF.
    FREE MEMORY.
    MESSAGE e398(00) WITH 'Curie list delivery failed:' lv_rfcmsg.
  ENDIF.

  FREE MEMORY.
