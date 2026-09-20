"""Registered RFC server: lets the SAP system call Python function modules.

The process registers PROGRAM_ID at the SAP gateway RFC_REG_COUNT times and
then waits. ABAP calls into it through the SM59 destination (connection type
T, "Registered Server Program") whose Program ID matches:

    CALL FUNCTION 'STFC_CONNECTION' DESTINATION 'SAP_AI'
      EXPORTING requtext = 'hello'
      IMPORTING echotext = lv_echo
                resptext = lv_resp.

The functions being served live in the handlers package, one module per topic;
importing it registers them. Their interfaces are read from the SAP system
over an ordinary client connection at startup, so the client parameters in
.env are needed even though this is a server.

This runs on NW RFC SDK 7.20 via sapnwrfc720.py rather than on pyrfc/7.50,
which cannot answer a pre-4.6C backend -- see that module's docstring. Nothing
here may import pyrfc, directly or through config, or the two SDKs would clash
in one process.

Usage:
    python -m curie.tools.rfc_server            # register and serve until Ctrl+C
    python -m curie.tools.rfc_server --check    # verify logon + handler metadata, then exit
    python -m curie.tools.rfc_dev               # the same, restarted when a file changes
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from curie import config
from curie import handlers
from curie import sapnwrfc720 as sap

log = logging.getLogger("rfc_server")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    if os.environ.get("RFC_SERVER_LOG"):
        handler = logging.FileHandler(os.environ["RFC_SERVER_LOG"], encoding="utf-8")
        handler.setFormatter(logging.root.handlers[0].formatter)
        logging.root.addHandler(handler)


def new_server() -> sap.Server:
    return sap.Server(
        config.SAP_RFC_SERVER,
        config.SAP_RFC,
        reg_count=config.RFC_REG_COUNT,
        dispatch_timeout=config.RFC_DISPATCH_TIMEOUT,
    )


def build_server() -> sap.Server:
    """Create the server and install every discovered handler."""
    if not config.SAP_RFC_SERVER["PROGRAM_ID"]:
        raise SystemExit("RFC_PROGID is not set in .env -- nothing to register")
    if not handlers.HANDLERS:
        raise SystemExit("no handlers found in the handlers package")

    server = new_server()
    server.on_error = lambda name, exc: log.exception(
        "%s handler raised %s", name, type(exc).__name__, exc_info=exc
    )
    for name, callback in handlers.HANDLERS.items():
        # Reads the function interface from SAP over the client connection.
        try:
            server.add_function(name, callback)
        except sap.RFCError as exc:
            if name not in handlers.OPTIONAL:
                raise
            # A plugin's module is missing in the backend: serve without it.
            log.error("optional %s not served: %s -- create it in SE37 (remote-enabled) "
                      "or switch the plugin off", name, exc)
            continue
        log.info("serving %s -> %s.%s()", name, callback.__module__, callback.__name__)
    return server


def check() -> int:
    """Verify the client logon and that every handler's FM exists in SAP."""
    log.info("NW RFC SDK %s from %s", sap.sdk_version(), sap.SDK_HOME)
    server = new_server()
    failed = []
    try:
        for name, callback in handlers.HANDLERS.items():
            try:
                server.function_desc(name)
                log.info("metadata ok: %-20s <- %s", name, callback.__module__)
            except sap.RFCError as exc:
                failed.append(name)
                log.error("metadata FAILED: %s -- %s", name, exc)
    finally:
        server.close()
    log.info(
        "would register PROGRAM_ID=%s (%sx) at %s/%s",
        config.SAP_RFC_SERVER["PROGRAM_ID"],
        config.RFC_REG_COUNT,
        config.SAP_RFC_SERVER["GWHOST"],
        config.SAP_RFC_SERVER["GWSERV"],
    )
    return 1 if failed else 0


def serve() -> int:
    log.info("NW RFC SDK %s from %s", sap.sdk_version(), sap.SDK_HOME)
    server = build_server()
    stopped = threading.Event()

    def request_stop(signum, _frame):
        log.info("%s received, shutting down", signal.Signals(signum).name)
        stopped.set()

    # SIGBREAK is what rfc_dev.py sends on Windows; SIGTERM is what a service
    # manager sends. Both mean "finish the call in flight and let go".
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)

    server.start()
    log.info(
        "registered PROGRAM_ID=%s (%sx) at gateway %s/%s -- waiting for calls "
        "(Ctrl+C to stop)",
        config.SAP_RFC_SERVER["PROGRAM_ID"],
        config.RFC_REG_COUNT,
        config.SAP_RFC_SERVER["GWHOST"],
        config.SAP_RFC_SERVER["GWSERV"],
    )
    try:
        # Polled rather than a plain wait(): on Windows a signal handler only
        # runs when the main thread executes bytecode, so an untimed wait()
        # would swallow Ctrl+C and SIGBREAK entirely.
        while not stopped.wait(0.5):
            pass
    except KeyboardInterrupt:  # Ctrl+C outside the signal handler
        log.info("interrupted, shutting down")
    finally:
        log.info("draining (up to %ds for calls in flight)",
                 config.RFC_DISPATCH_TIMEOUT)
        server.stop()
        server.close()
        log.info("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify logon and handler metadata without registering",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    try:
        return check() if args.check else serve()
    except sap.RFCError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
