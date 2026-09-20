REPORT zai_start.
*----------------------------------------------------------------------*
* Curie: entry point for end users (transaction ZAI).
*
* 1. RFC_PING proves that Curie is registered as SAP_AI.
* 2. Z_AI_SESSION_OPEN returns session id, URL, heartbeat interval.
* 3. Chrome is started on the frontend in --app mode with GUI_RUN.
*    WS_EXECUTE is NOT used: its command line is cut at 128
*    characters, which is shorter than the Chrome flags. GUI_RUN
*    takes a 255-character parameter string and the frontend
*    expands %TEMP% itself, so no WS_QUERY is needed.
* 4. The dialog session stays in a heartbeat loop until Curie
*    answers CLOSED or the connection breaks. WAIT UP TO rolls the
*    work process out, so rdisp/max_wproc_runtime is not hit.
*
* ECC 6.0 alternative for step 3 (deliberately NOT used here so the
* same source compiles on 4.5B; GUI_RUN still works on ECC):
*
*   CALL METHOD cl_gui_frontend_services=>execute
*     EXPORTING
*       application            = p_chrome
*       parameter              = lv_args
*       minimized              = 'N'
*     EXCEPTIONS
*       cntl_error             = 1
*       error_no_gui           = 2
*       bad_parameter          = 3
*       file_not_found         = 4
*       path_not_found         = 5
*       file_extension_unknown = 6
*       error_execute_failed   = 7
*       synchronous_failed     = 8
*       not_supported_by_gui   = 9
*       OTHERS                 = 10.
*
*   The SAP GUI security module may ask the user once to allow
*   starting chrome.exe; answer "Allow" / "Always allow".
*----------------------------------------------------------------------*

PARAMETERS: p_prompt(255) TYPE c LOWER CASE,
            p_chrome(255) TYPE c LOWER CASE DEFAULT
 'C:\Program Files\Google\Chrome\Application\chrome.exe'.

DATA: lv_sess(32)  TYPE c,
      lv_url(255)  TYPE c,
      lv_state(16) TYPE c,
      lv_text(80)  TYPE c,
      lv_cmd(255)  TYPE c,
      lv_args(255) TYPE c,
      lv_msg(80)   TYPE c,
      lv_hb        TYPE i,
      lv_rc        TYPE i.

* Fragments of the Chrome command line, joined with SEPARATED BY
* space. GUI_RUN's PARAMETER is 255 characters: with a 32-character
* session id the line below is about 205 characters long.
CONSTANTS:
  c_dest(6)   TYPE c VALUE 'SAP_AI',
  c_app(7)    TYPE c VALUE '--app="',
  c_quote(1)  TYPE c VALUE '"',
  c_udd(45)   TYPE c
    VALUE '--user-data-dir="%TEMP%\chrome-kiosk-profile"',
  c_size(22)  TYPE c VALUE '--window-size=1024,768',
  c_nofr(14)  TYPE c VALUE '--no-first-run',
  c_nodb(26)  TYPE c VALUE '--no-default-browser-check',
  c_notr(28)  TYPE c VALUE '--disable-features=Translate',
  c_closed(6) TYPE c VALUE 'CLOSED'.

START-OF-SELECTION.

*---------------------------------------------------------------------*
* 1. Is Curie registered at the gateway?
*---------------------------------------------------------------------*
  CLEAR lv_msg.
  CALL FUNCTION 'RFC_PING' DESTINATION c_dest
       EXCEPTIONS
            communication_failure = 1 MESSAGE lv_msg
            system_failure        = 2 MESSAGE lv_msg
            OTHERS                = 3.
  IF sy-subrc <> 0.
    MESSAGE e398(00) WITH 'Curie not available:' lv_msg.
  ENDIF.

*---------------------------------------------------------------------*
* 2. Open the session; Curie takes user/client/system from the
*    RFC connection attributes.
*---------------------------------------------------------------------*
  CLEAR: lv_sess, lv_url, lv_hb, lv_msg.
  CALL FUNCTION 'Z_AI_SESSION_OPEN' DESTINATION c_dest
       EXPORTING
            i_prompt              = p_prompt
       IMPORTING
            e_session_id          = lv_sess
            e_url                 = lv_url
            e_heartbeat_sec       = lv_hb
       EXCEPTIONS
            server_busy           = 1
            not_allowed           = 2
            communication_failure = 3 MESSAGE lv_msg
            system_failure        = 4 MESSAGE lv_msg
            OTHERS                = 5.
  CASE sy-subrc.
    WHEN 0.
    WHEN 1.
      MESSAGE e398(00) WITH 'Curie is busy, try again later'.
    WHEN 2.
      MESSAGE e398(00) WITH 'Curie: session not allowed'.
    WHEN OTHERS.
      MESSAGE e398(00) WITH 'Session could not be opened' lv_msg.
  ENDCASE.
  IF lv_url IS INITIAL OR lv_sess IS INITIAL.
    MESSAGE e398(00) WITH 'Curie returned no session URL'.
  ENDIF.
  IF lv_hb IS INITIAL.
    lv_hb = 3.
  ENDIF.

*---------------------------------------------------------------------*
* 3. Start Chrome on the frontend PC (GUI_RUN, not WS_EXECUTE: the
*    latter cuts the command line at 128 characters).
*---------------------------------------------------------------------*
* The executable path is quoted because it contains blanks.
  CONCATENATE c_quote p_chrome c_quote INTO lv_cmd.

* --app="<url>" first; the URL has no blanks, so the pieces of the
* first flag are joined without separator, then the rest with blanks.
  CONCATENATE c_app lv_url c_quote INTO lv_args.
  CONCATENATE lv_args c_size c_udd c_nofr c_nodb c_notr
              INTO lv_args SEPARATED BY space.

  CLEAR lv_rc.
  CALL FUNCTION 'GUI_RUN'
       EXPORTING
            command    = lv_cmd
            parameter  = lv_args
       IMPORTING
            returncode = lv_rc
       EXCEPTIONS
            OTHERS     = 1.
  IF sy-subrc <> 0 OR lv_rc <> 0.
    IF lv_rc = 0.
      lv_rc = sy-subrc.
    ENDIF.
    CALL FUNCTION 'Z_AI_SESSION_CLOSE' DESTINATION c_dest
         EXPORTING
              i_session_id = lv_sess
              i_reason     = 'CHROME_START_FAILED'
         EXCEPTIONS
              OTHERS       = 1.
    MESSAGE e398(00) WITH 'Chrome could not be started, rc' lv_rc
                          p_chrome(40).
  ENDIF.

*---------------------------------------------------------------------*
* 4. Heartbeat loop. Curie ends it by answering CLOSED (browser
*    window closed, idle timeout, shutdown); a failed call (Curie
*    gone) ends it as well.
*---------------------------------------------------------------------*
  CLEAR: lv_state, lv_text, lv_msg, lv_rc.
  DO.
    CALL FUNCTION 'Z_AI_HEARTBEAT' DESTINATION c_dest
         EXPORTING
              i_session_id          = lv_sess
         IMPORTING
              e_state               = lv_state
              e_status_text         = lv_text
         EXCEPTIONS
              unknown_session       = 1
              communication_failure = 2 MESSAGE lv_msg
              system_failure        = 3 MESSAGE lv_msg
              OTHERS                = 4.
    lv_rc = sy-subrc.
    IF lv_rc <> 0 OR lv_state = c_closed.
      EXIT.
    ENDIF.
    IF lv_text IS INITIAL.
      lv_text = 'Curie session active'.
    ENDIF.
    CALL FUNCTION 'SAPGUI_PROGRESS_INDICATOR'
         EXPORTING
              text = lv_text.
    WAIT UP TO lv_hb SECONDS.
  ENDDO.

*---------------------------------------------------------------------*
* 5. Done.
*---------------------------------------------------------------------*
  IF lv_rc = 0.
    MESSAGE s398(00) WITH 'Curie session ended'.
  ELSEIF lv_rc = 1.
    MESSAGE s398(00) WITH 'Curie session ended (unknown session)'.
  ELSE.
    MESSAGE s398(00) WITH 'Curie connection lost:' lv_msg.
  ENDIF.
