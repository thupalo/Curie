"""HTTP + Server-Sent Events for the Chrome window.

    GET  /s/{token}                     the page (static/index.html)
    GET  /s/{token}/events              SSE: transcript replay, then live events
    POST /s/{token}/prompt  {text}      start a turn (409 while one runs);
                                        "/reset" clears the conversation instead
    POST /s/{token}/reset               the same, as an endpoint
    POST /s/{token}/stop                cancel the current turn
    POST /s/{token}/runs/{id}/confirm   run the checked program
    POST /s/{token}/runs/{id}/discard   drop it
    POST /s/{token}/ping                browser liveness
    POST /s/{token}/close               window closed (sendBeacon)
    GET  /healthz

The token in the URL is the only credential; unknown tokens are 404, closed
sessions 410.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from curie import caches
from curie import config
from curie.sessions.model import Session, State

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
SSE_KEEPALIVE = 15.0


def create_app(manager, orchestrator, flow, executor=None) -> FastAPI:
    app = FastAPI(title="Curie", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def session_or_404(token: str, request: Request | None = None,
                       allow_closed: bool = False) -> Session:
        session = manager.get(token)
        if session is None:
            raise HTTPException(404, "unknown session")
        if request is not None:
            ip = request.client.host if request.client else None
            if not manager.bind_client(session, ip):
                raise HTTPException(403, "session belongs to another client")
        if session.closed and not allow_closed:
            raise HTTPException(410, "session closed")
        return session

    @app.get("/healthz")
    async def healthz():
        out = {"ok": True, "sessions": len(manager.open_sessions()), "caches": caches.stats()}
        llm = getattr(orchestrator, "llm", None)
        if getattr(llm, "last_check", None):
            out["llm"] = {k: llm.last_check[k] for k in ("ok", "model", "base_url", "error", "served")}
        from curie.chat import service as chat_service
        chat = chat_service.current()
        if chat is not None:
            out["chat"] = chat.stats()
        knowledge = getattr(orchestrator, "knowledge", None)
        if knowledge is not None and hasattr(knowledge, "stats"):
            out["knowledge"] = knowledge.stats()
        return out

    @app.post("/admin/knowledge/reload")
    async def knowledge_reload(request: Request):
        """Take the compiled bundle from disk again (after the worker wrote it)."""
        ip = request.client.host if request.client else ""
        if ip not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "allowed from the Curie host only")
        knowledge = getattr(orchestrator, "knowledge", None)
        if knowledge is None or not hasattr(knowledge, "reload_bundle"):
            raise HTTPException(503, "knowledge is off")
        return {"ok": True, "bundle": knowledge.reload_bundle()}

    @app.post("/admin/cache/flush")
    async def cache_flush(request: Request):
        # Operator action: only from the Curie host itself.
        ip = request.client.host if request.client else ""
        if ip not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "flush is allowed from the Curie host only")
        flushed = caches.flush_all()          # memory and disk
        log.info("caches flushed from %s: %s", ip, flushed)
        return {"ok": True, "flushed": flushed}

    @app.post("/admin/cache/reload")
    async def cache_reload(request: Request):
        """Take the persisted caches from disk again (after `curie --cache warm`)."""
        ip = request.client.host if request.client else ""
        if ip not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "reload is allowed from the Curie host only")
        loaded = caches.load_all()
        log.info("caches reloaded from %s: %s", ip, loaded)
        return {"ok": True, "loaded": loaded}

    @app.post("/admin/knowledge/review")
    async def knowledge_review(request: Request):
        """Run the knowledge review now (loopback only); ?apply=1 also applies fixes."""
        ip = request.client.host if request.client else ""
        if ip not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "allowed from the Curie host only")
        reviewer = getattr(app.state, "reviewer", None)
        if reviewer is None:
            raise HTTPException(503, "knowledge is off or the reviewer is not built")
        apply = request.query_params.get("apply") in ("1", "true", "yes")
        everything = request.query_params.get("all") in ("1", "true", "yes")
        report = await reviewer.run(apply=apply, everything=everything)
        return {"ok": True, **report}

    @app.get("/admin/knowledge/review")
    async def knowledge_review_last(request: Request):
        ip = request.client.host if request.client else ""
        if ip not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "allowed from the Curie host only")
        reviewer = getattr(app.state, "reviewer", None)
        return {"ok": True, "last": reviewer.last if reviewer is not None else None}

    if config.CURIE_DEV:
        log.warning("CURIE_DEV=1: POST /dev/open creates sessions without SAP GUI")

        @app.post("/dev/open")
        async def dev_open(request: Request):
            body = await _json(request)
            session = manager.open_dev(str(body.get("prompt", "")), str(body.get("user", "")))
            return {"session_id": session.id, "url": manager.url_for(session)}

    @app.get("/s/{token}")
    async def page(token: str, request: Request):
        session_or_404(token, request, allow_closed=True)
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/s/{token}/events")
    async def events(token: str, request: Request):
        session = session_or_404(token, request, allow_closed=True)
        if not session.closed:
            manager.touch_browser(session)
        queue = manager.subscribe(session)

        async def gen():
            try:
                while True:
                    try:
                        event, data = await asyncio.wait_for(queue.get(), SSE_KEEPALIVE)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if not session.closed:
                        manager.touch_browser(session)
                    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
                    if event == "session_closed":
                        return
            finally:
                manager.unsubscribe(session, queue)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive",
        })

    @app.post("/s/{token}/prompt")
    async def prompt(token: str, request: Request):
        session = session_or_404(token, request)
        body = await _json(request)
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(400, "empty prompt")
        if len(text) > 20000:
            raise HTTPException(413, "prompt too long")
        command = orchestrator.command(session, text)
        if command:
            return {"ok": True, "command": command}
        if not orchestrator.start_turn(session, text):
            raise HTTPException(409, "a turn is already running")
        return JSONResponse({"ok": True}, status_code=202)

    @app.post("/s/{token}/reset")
    async def reset(token: str, request: Request):
        session = session_or_404(token, request)
        orchestrator.command(session, "/reset")
        return {"ok": True}

    @app.post("/s/{token}/stop")
    async def stop(token: str, request: Request):
        session = session_or_404(token, request)
        stopped = orchestrator.stop(session)
        return {"ok": True, "stopped": stopped}

    @app.post("/s/{token}/runs/{run_id}/confirm")
    async def confirm(token: str, run_id: str, request: Request):
        session = session_or_404(token, request)
        if not flow.confirm(session, run_id):
            raise HTTPException(409, "nothing to run")
        return JSONResponse({"ok": True}, status_code=202)

    @app.post("/s/{token}/runs/{run_id}/discard")
    async def discard(token: str, run_id: str, request: Request):
        session = session_or_404(token, request)
        if not flow.discard(session, run_id):
            raise HTTPException(409, "nothing to discard")
        return {"ok": True}

    @app.post("/s/{token}/feedback")
    async def feedback(token: str, request: Request):
        """Thumbs up or down on an answer or a run result: the utility
        signal for the notes that were in the prompt."""
        session = session_or_404(token, request)
        body = await _json(request)
        target = str(body.get("target", "")).strip()
        ident = str(body.get("id", "")).strip()
        value = str(body.get("value", "")).strip().lower()
        if target not in ("answer", "run") or not ident or value not in ("up", "down"):
            raise HTTPException(400, "target answer|run, id, value up|down")
        if config.KNOWLEDGE_FEEDBACK == "off":
            return {"ok": False, "text": "Feedback is switched off on this installation."}
        text = orchestrator.feedback(session, target, ident, value)
        return {"ok": True, "text": text}

    @app.post("/s/{token}/ping")
    async def ping(token: str, request: Request):
        session = session_or_404(token, request)
        manager.touch_browser(session)
        return {"ok": True, "state": session.state.value}

    @app.post("/s/{token}/close")
    async def close(token: str, request: Request):
        session = manager.get(token)
        if session is not None and not session.closed:
            ip = request.client.host if request.client else None
            if manager.bind_client(session, ip):
                manager.close(session, "browser_gone")
        return {"ok": True}

    return app


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}
