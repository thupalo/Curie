"""Development runner: restarts Curie whenever a source file changes.

The same shape as `uvicorn --reload`: this process supervises, a child process
serves. On a change the child is asked to stop, drains its in-flight call,
closes its gateway registrations, and a fresh one is started.

A child process rather than importlib.reload() on purpose. The SDK holds the
gateway registrations and the installed callback pointers process-wide, with
no API to uninstall a function -- reloading modules in place would leave the
old callbacks live and the registrations pointing at stale code. Restarting
the process is the only way to get a clean slate, which is why uvicorn does
the same.

    python -m curie.tools.rfc_dev                 # watch the package and .env
    python -m curie.tools.rfc_dev --interval 2    # slower polling
    python -m curie.tools.rfc_dev --module curie.tools.rfc_server
    python -m curie.tools.rfc_dev -- -v           # arguments for the module

Not for production: the reloader adds a process and polls the filesystem. Run
rfc_server.py directly there, under whatever supervises your services.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]          # src/curie
HOME = Path(os.environ.get("CURIE_HOME") or Path.cwd()).resolve()

# Everything whose change should restart the server: every directory of the
# package, and .env because it carries the connection and REG_COUNT settings.
IGNORE_DIRS = {"venv", "__pycache__", ".git", "Trace"}
WATCH_DIRS = [PACKAGE_DIR] + [p for p in PACKAGE_DIR.rglob("*")
                              if p.is_dir() and p.name not in IGNORE_DIRS]
WATCH_FILES = [HOME / ".env", HOME.parent / ".env"]

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def snapshot() -> dict:
    """Modification times of everything being watched."""
    stamps = {}
    for directory in WATCH_DIRS:
        if not directory.is_dir() or directory.name in IGNORE_DIRS:
            continue
        for path in directory.glob("*.py"):
            try:
                stamps[path] = path.stat().st_mtime
            except OSError:
                pass
    for path in WATCH_FILES:
        try:
            stamps[path] = path.stat().st_mtime
        except OSError:
            pass
    return stamps


def changed(before: dict, after: dict) -> list:
    names = []
    for path, stamp in after.items():
        if before.get(path) != stamp:
            names.append(path.name)
    names += [p.name for p in before if p not in after]
    return sorted(names)


def start_child(server_args: list, module: str = "curie.cli") -> subprocess.Popen:
    cmd = [sys.executable, "-u", "-m", module, *server_args]
    # A new process group so CTRL_BREAK reaches the child alone, and not this
    # supervisor as well.
    return subprocess.Popen(cmd, cwd=HOME, creationflags=CREATE_NEW_PROCESS_GROUP)


def stop_child(child: subprocess.Popen, grace: float) -> None:
    """Ask the child to shut down, and insist if it will not."""
    if child.poll() is not None:
        return
    try:
        if os.name == "nt":
            # The child installs a SIGBREAK handler; this is the only signal
            # Windows lets us deliver to another process's group.
            child.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            child.send_signal(signal.SIGTERM)
    except (OSError, ValueError):
        child.kill()
        return
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        print(f"[reload] child did not stop within {grace}s, killing it", flush=True)
        child.kill()
        child.wait(timeout=5)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between checks (default 1)")
    parser.add_argument("--grace", type=float, default=20.0,
                        help="seconds to wait for a clean shutdown (default 20)")
    parser.add_argument("--module", default="curie.cli",
                        help="module to supervise with python -m (default curie.cli; "
                             "curie.tools.rfc_server for the bare RFC server)")
    parser.add_argument("server_args", nargs="*",
                        help="arguments passed through to the script")
    args = parser.parse_args(argv)

    print(f"[reload] watching {', '.join(str(d) for d in WATCH_DIRS)}", flush=True)
    stamps = snapshot()
    child = start_child(args.server_args, args.module)
    try:
        while True:
            time.sleep(args.interval)
            if child.poll() is not None:
                # The server exited by itself: a startup error, most likely a
                # handler whose function module is missing. Wait for the edit
                # that fixes it instead of restarting in a loop.
                print(f"[reload] server exited with {child.returncode}, "
                      "waiting for a change", flush=True)
                while True:
                    time.sleep(args.interval)
                    current = snapshot()
                    if changed(stamps, current):
                        break
                    stamps = current
            current = snapshot()
            touched = changed(stamps, current)
            if not touched:
                continue
            stamps = current
            print(f"[reload] {', '.join(touched)} changed, restarting", flush=True)
            stop_child(child, args.grace)
            child = start_child(args.server_args, args.module)
    except KeyboardInterrupt:
        print("[reload] stopping", flush=True)
        stop_child(child, args.grace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
