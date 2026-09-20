"""Curie: LLM chat inside SAP over RFC (R/3 4.5B and ECC).

    curie              serve (python -m curie does the same)
    curie --check      verify SAP logon, function module interfaces, the LLM
    curie --selftest   with Curie running: round trips from inside SAP

Code lives in this package; the project directory (CURIE_HOME, default the
current directory) holds .env, knowledge/, logs/ and the SAP sources.
"""
__version__ = "1.0.0"
