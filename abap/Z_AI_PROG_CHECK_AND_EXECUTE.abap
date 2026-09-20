FUNCTION z_ai_prog_check_and_execute.
*"----------------------------------------------------------------------
*"*"Local interface:
*"  IMPORTING
*"     VALUE(I_SESSION_ID) LIKE  ZAI_PARAMS-SESSION_ID
*"     VALUE(I_RUN_ID) LIKE  ZAI_PARAMS-RUN_ID
*"     VALUE(I_PROGNAME) LIKE  SY-REPID
*"     VALUE(I_USER) LIKE  SY-UNAME OPTIONAL
*"     VALUE(I_MODE) LIKE  ZAI_PARAMS-MODE DEFAULT 'C'
*"  EXPORTING
*"     VALUE(E_SYNTAX_OK) LIKE  ZAI_PARAMS-SYNTAX_OK
*"     VALUE(E_MESSAGE) LIKE  ZAI_PARAMS-MESSAGE
*"     VALUE(E_LINE) TYPE  I
*"     VALUE(E_WORD) LIKE  ZAI_PARAMS-WORD
*"     VALUE(E_JOBNAME) LIKE  TBTCO-JOBNAME
*"     VALUE(E_JOBCOUNT) LIKE  TBTCO-JOBCOUNT
*"  TABLES
*"      T_SOURCE STRUCTURE  ZAI_LINE
*"      T_LIST STRUCTURE  ZAI_LISTLINE OPTIONAL
*"  EXCEPTIONS
*"      INSERT_FAILED
*"      JOB_FAILED
*"      NOT_AUTHORIZED
*"      PROGNAME_INVALID
*"----------------------------------------------------------------------
*----------------------------------------------------------------------*
* Curie: syntax check / execute / delete a generated ZAI_* program.
*
* I_MODE  'C'  syntax check only (read-only, nothing is created)
*         'X'  check + INSERT REPORT + background job ZAI_RUNNER
*              running as I_USER (list is pushed back to Curie by
*              ZAI_RUNNER through Z_AI_LIST_DELIVER)
*         'S'  check + INSERT REPORT + synchronous SUBMIT, list is
*              returned in T_LIST, program is deleted again
*         'D'  DELETE REPORT only
*
* The first line of T_SOURCE must be "REPORT <I_PROGNAME>."
* (Curie injects it).  Remote-enabled; called by Curie's RFC user.
*----------------------------------------------------------------------*

  DATA: lv_mess(255) TYPE c,
        lv_lin       TYPE i,
        lv_wrd(72)   TYPE c,
        lv_user      LIKE sy-uname,
        lv_jobname   LIKE tbtco-jobname,
        lv_jobcount  LIKE tbtco-jobcount.

  CLEAR: e_syntax_ok, e_message, e_line, e_word,
         e_jobname, e_jobcount.
  REFRESH t_list.

*---------------------------------------------------------------------*
* Only programs named ZAI_* may be touched by this module.
*---------------------------------------------------------------------*
  IF i_progname IS INITIAL OR i_progname(4) <> 'ZAI_'.
    RAISE progname_invalid.
  ENDIF.

*---------------------------------------------------------------------*
* Modes that create or delete a program object need S_DEVELOP.
* INSERT/DELETE REPORT do not check authorizations themselves.
*---------------------------------------------------------------------*
  IF i_mode = 'X' OR i_mode = 'S' OR i_mode = 'D'.
    AUTHORITY-CHECK OBJECT 'S_DEVELOP'
             ID 'DEVCLASS' DUMMY
             ID 'OBJTYPE'  FIELD 'PROG'
             ID 'OBJNAME'  FIELD i_progname
             ID 'P_GROUP'  DUMMY
             ID 'ACTVT'    FIELD '02'.
    IF sy-subrc <> 0.
      RAISE not_authorized.
    ENDIF.
  ENDIF.

*---------------------------------------------------------------------*
* Mode 'D': delete only.
*---------------------------------------------------------------------*
  IF i_mode = 'D'.
    DELETE REPORT i_progname.
    e_message = 'Program deleted'.
    EXIT.
  ENDIF.

*---------------------------------------------------------------------*
* Syntax check of the source table (all remaining modes).
*---------------------------------------------------------------------*
  SYNTAX-CHECK FOR t_source MESSAGE lv_mess
                            LINE    lv_lin
                            WORD    lv_wrd
                            PROGRAM i_progname.
  IF sy-subrc <> 0.
    e_message = lv_mess.
    e_line    = lv_lin.
    e_word    = lv_wrd.
    EXIT.
  ENDIF.
  e_syntax_ok = 'X'.

  IF i_mode <> 'X' AND i_mode <> 'S'.
    EXIT.
  ENDIF.

*---------------------------------------------------------------------*
* Create the program object (local object, $TMP).
*---------------------------------------------------------------------*
  INSERT REPORT i_progname FROM t_source.
  IF sy-subrc <> 0.
    RAISE insert_failed.
  ENDIF.

*---------------------------------------------------------------------*
* Mode 'S': run synchronously as the RFC user, list into T_LIST.
* A short dump in the program comes back to the caller as
* RFC_ABAP_RUNTIME_FAILURE; the program is then left for the
* Curie sweep (mode 'D').
*---------------------------------------------------------------------*
  IF i_mode = 'S'.
    SUBMIT (i_progname) EXPORTING LIST TO MEMORY AND RETURN.
    PERFORM list_to_table TABLES t_list.
    DELETE REPORT i_progname.
    e_message = 'Executed synchronously'.
    EXIT.
  ENDIF.

*---------------------------------------------------------------------*
* Mode 'X': background job ZAI_RUNNER under the end user.
*---------------------------------------------------------------------*
  IF i_user IS INITIAL.
    lv_user = sy-uname.
  ELSE.
    lv_user = i_user.
  ENDIF.

  lv_jobname = i_progname.
  CALL FUNCTION 'JOB_OPEN'
       EXPORTING
            jobname  = lv_jobname
       IMPORTING
            jobcount = lv_jobcount
       EXCEPTIONS
            OTHERS   = 1.
  IF sy-subrc <> 0.
    e_message = 'JOB_OPEN failed'.
    RAISE job_failed.
  ENDIF.

  SUBMIT zai_runner WITH p_prog = i_progname
                    WITH p_sess = i_session_id
                    WITH p_run  = i_run_id
                    USER lv_user
                    VIA JOB lv_jobname NUMBER lv_jobcount
                    AND RETURN.
  IF sy-subrc <> 0.
    e_message = 'SUBMIT VIA JOB failed (S_BTCH_NAM for user?)'.
    RAISE job_failed.
  ENDIF.

  CALL FUNCTION 'JOB_CLOSE'
       EXPORTING
            jobcount  = lv_jobcount
            jobname   = lv_jobname
            strtimmed = 'X'
       EXCEPTIONS
            OTHERS    = 1.
  IF sy-subrc <> 0.
    e_message = 'JOB_CLOSE failed (free BTC work process?)'.
    RAISE job_failed.
  ENDIF.

  e_jobname  = lv_jobname.
  e_jobcount = lv_jobcount.
  e_message  = 'Job released'.

ENDFUNCTION.

*----------------------------------------------------------------------*
* The FORM below stays here, after ENDFUNCTION, as part of the module's
* source: paste or upload this whole file as the source code of
* Z_AI_PROG_CHECK_AND_EXECUTE in SE37, save and activate. No separate
* include is needed.
*----------------------------------------------------------------------*
*&---------------------------------------------------------------------*
*&      Form  LIST_TO_TABLE
*&---------------------------------------------------------------------*
*       Reads the list of the last SUBMIT ... EXPORTING LIST TO
*       MEMORY and converts it to ASCII lines (one per table row).
*----------------------------------------------------------------------*
*      -->PT_LIST  target table (ZAI_LISTLINE)
*----------------------------------------------------------------------*
FORM list_to_table TABLES pt_list STRUCTURE zai_listline.
  DATA: lt_obj  LIKE abaplist  OCCURS 0 WITH HEADER LINE,
        lt_asci LIKE listzeile OCCURS 0 WITH HEADER LINE.

  REFRESH pt_list.

  CALL FUNCTION 'LIST_FROM_MEMORY'
       TABLES
            listobject = lt_obj
       EXCEPTIONS
            OTHERS     = 1.
  IF sy-subrc = 0.
    CALL FUNCTION 'LIST_TO_ASCI'
         EXPORTING
              list_index = -1
         TABLES
              listasci   = lt_asci
              listobject = lt_obj
         EXCEPTIONS
              OTHERS     = 1.
    IF sy-subrc = 0.
      LOOP AT lt_asci.
        pt_list-line = lt_asci.
        APPEND pt_list.
      ENDLOOP.
    ENDIF.
  ENDIF.

  FREE MEMORY.
ENDFORM.
