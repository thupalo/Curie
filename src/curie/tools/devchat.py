"""Drive a running Curie (CURIE_DEV=1) from the command line, without SAP GUI.

    python -m curie.tools.devchat "how many clients are in T000?"
    python -m curie.tools.devchat --run "list 5 materials of type FERT with an ABAP program"
    python -m curie.tools.devchat --url http://localhost:8765 "..."

Opens a session through POST /dev/open, sends the prompt, prints the SSE
events as they arrive, and -- with --run -- confirms the first runnable code
card and waits for the program's list. Exits when the session returns to
IDLE (or CONFIRM without --run).
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

import httpx


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt")
    parser.add_argument("--url", default="http://localhost:8765")
    parser.add_argument("--run", action="store_true", help="confirm runnable code")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    base = args.url.rstrip("/")
    r = httpx.post(f"{base}/dev/open", json={"prompt": args.prompt}, timeout=10)
    r.raise_for_status()
    sid = r.json()["session_id"]
    s = f"{base}/s/{sid}"
    print(f"session {sid}  {r.json()['url']}")

    stop = threading.Event()

    def pinger():
        while not stop.is_set():
            try:
                httpx.post(f"{s}/ping", timeout=5)
            except httpx.HTTPError:
                pass
            stop.wait(3)

    threading.Thread(target=pinger, daemon=True).start()

    deadline = time.time() + args.timeout
    confirmed = False
    started = False
    rc = 1
    with httpx.stream("GET", f"{s}/events", timeout=httpx.Timeout(None)) as stream:
        event = None
        sent = False
        for line in stream.iter_lines():
            if time.time() > deadline:
                print("timeout")
                break
            if line.startswith("event: "):
                event = line[7:]
                continue
            if not line.startswith("data: "):
                continue
            data = json.loads(line[6:])
            if event == "hello":
                print(f"[hello] {data['sap']} state={data['state']}")
                if not sent:
                    httpx.post(f"{s}/prompt", json={"text": args.prompt}, timeout=10).raise_for_status()
                    sent = True
            elif event == "assistant_delta":
                sys.stdout.write(data["text"])
                sys.stdout.flush()
            elif event == "assistant_start":
                sys.stdout.write("\n[assistant] ")
            elif event == "assistant_done":
                print(f"\n[done{' interrupted' if data.get('interrupted') else ''}]")
            elif event == "tool_call":
                print(f"\n[tool] {data['name']} {json.dumps(data['args'], ensure_ascii=False)}")
            elif event == "tool_result":
                print(f"[tool] {'ok' if data['ok'] else 'ERROR'} {data['ms']} ms: {data['summary'][:300]}")
            elif event == "syntax_error":
                print(f"\n[syntax] line {data['line']} {data['word']!r}: {data['message']} "
                      f"(attempt {data['attempt']}/{data['max_attempts']})")
            elif event == "code_ready":
                audit = data.get("audit") or {}
                print(f"\n[code] run {data['run_id']} {data['progname']} can_run={data['can_run']} "
                      f"syntax={data.get('syntax')} audit={audit.get('allowed')} risk={audit.get('risk')} "
                      f"{audit.get('summary', '')}")
                for v in audit.get("violations", []):
                    print(f"       - {v}")
                print("-" * 72)
                print(data["source"])
                print("-" * 72)
                if args.run and data["can_run"] and not confirmed:
                    confirmed = True
                    httpx.post(f"{s}/runs/{data['run_id']}/confirm", timeout=10).raise_for_status()
                    print("[confirmed]")
            elif event == "run_started":
                print(f"[run] started mode={data['mode']} job={data['job']}")
            elif event == "run_result":
                print(f"[run] ok={data['ok']} rc={data['rc']} {data['message']}")
                for l in data["lines"]:
                    print("   |" + l)
            elif event == "notice":
                print(f"\n[{data['level']}] {data['text']}")
            elif event == "state":
                print(f"[state] {data['state']} {data['text']}")
                if data["state"] in ("THINKING", "TOOL", "CHECKING", "RUNNING"):
                    started = True
                # The replayed IDLE right after hello is not the end of the turn.
                if started and data["state"] == "IDLE":
                    rc = 0
                    break
                if started and data["state"] == "CONFIRM" and not args.run:
                    rc = 0
                    break
            elif event == "session_closed":
                print(f"[closed] {data['reason']}")
                break
    stop.set()
    httpx.post(f"{s}/close", timeout=5)
    return rc


if __name__ == "__main__":
    sys.exit(main())
