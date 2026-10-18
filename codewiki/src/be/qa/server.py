"""``codewiki serve``: the docs site plus a local Q&A API for its chat window.

The site may be opened from this server (same origin) or from GitHub Pages;
either way the browser talks to this process on the developer's machine,
which answers with the user's own CodeWiki config. No key reaches the page.

Because any web page can try to reach ``localhost``, every ``/api`` request
whose ``Origin`` is not on the allow-list (this server, plus the site's
GitHub Pages origin and any ``--allow-origin``) is refused, so other sites
cannot spend the user's LLM quota or read their code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Iterable
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from codewiki import __version__
from codewiki.src.be.qa import get_engine, get_index, resolve_docs_dir
from codewiki.src.be.qa.engine import AskCancelled
from codewiki.src.be.qa.retrieve import Retriever

logger = logging.getLogger(__name__)

# Never served: the dependency graph / caches under temp/ and dotfiles.
_HIDDEN_PREFIXES = ("temp/", ".")


class AskRequest(BaseModel):
    question: str
    session_id: str | None = None
    max_context_tokens: int | None = None
    # Turns of the session to keep before answering (edit / retry an earlier question).
    keep_turns: int | None = Field(None, ge=0)


class SearchRequest(BaseModel):
    query: str
    k: int = 10
    kinds: str | None = None


def origin_of(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def default_origins(host: str, port: int, docs_dir: str) -> set[str]:
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://{host}:{port}"}
    try:
        from pathlib import Path

        from codewiki.cli.html_generator import HTMLGenerator

        index = get_index(docs_dir)
        info = HTMLGenerator().detect_repository_info(Path(index.repo_path or docs_dir))
        pages = origin_of(info.get("github_pages_url"))
        if pages:
            origins.add(pages)
    except Exception as e:  # noqa: BLE001 — the Pages origin is a convenience
        logger.debug("No GitHub Pages origin detected: %s", e)
    return {o.lower() for o in origins}


def create_app(
    docs_dir: str,
    allowed_origins: Iterable[str],
    *,
    model: str | None = None,
    log_questions: bool = True,
    engine_factory=None,
) -> FastAPI:
    docs_dir = resolve_docs_dir(docs_dir)
    allowed = {o.lower().rstrip("/") for o in allowed_origins}
    make_engine = engine_factory or (
        lambda: get_engine(docs_dir, model=model, log_questions=log_questions)
    )
    app = FastAPI(title="CodeWiki", docs_url=None, redoc_url=None, openapi_url=None)

    def cors_headers(origin: str | None) -> dict[str, str]:
        if not origin or origin not in allowed:
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Private-Network": "true",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }

    @app.middleware("http")
    async def guard(request: Request, call_next):
        origin = (request.headers.get("origin") or "").lower() or None
        if request.url.path.startswith("/api/"):
            # No Origin = not a cross-site browser request (curl, the same page's GET).
            if origin is not None and origin not in allowed:
                return JSONResponse(
                    {"error": f"origin {origin} is not allowed; restart with --allow-origin"},
                    status_code=403,
                )
            if request.method == "OPTIONS":
                return Response(status_code=204, headers=cors_headers(origin))
        response = await call_next(request)
        for key, value in cors_headers(origin).items():
            response.headers[key] = value
        return response

    @app.get("/api/health")
    def health():
        index = get_index(docs_dir)
        f = index.freshness
        return {
            "ok": True,
            "version": __version__,
            "repo": os.path.basename(index.repo_path or docs_dir),
            "freshness": {
                "summary": f.summary(),
                "docs_commit": f.docs_commit,
                "commits_behind": f.commits_behind,
                "changed_files": sorted(f.touched),
                "stale_pages": sorted(index.stale_pages),
            },
        }

    @app.post("/api/search")
    def search(req: SearchRequest):
        from codewiki.mcp.tools.qa import _kinds

        index = get_index(docs_dir)
        retriever = Retriever(index)
        try:
            cands = retriever.search(req.query, k=max(1, min(req.k, 50)), kinds=_kinds(req.kinds))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"results": [dict(retriever.describe(c.id), score=c.score) for c in cands]}

    @app.post("/api/ask")
    async def ask(req: AskRequest):
        if not req.question.strip():
            raise HTTPException(400, "question is required")
        try:
            engine = make_engine()
        except Exception as e:  # noqa: BLE001 — config errors go to the chat window
            raise HTTPException(503, str(e)) from e
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stopped = threading.Event()

        def emit(event: dict) -> None:
            if stopped.is_set():  # the client left; the loop may be gone too
                return
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:  # event loop closed: the server is shutting down
                pass

        def work() -> None:
            try:
                result = engine.ask(
                    req.question,
                    session_id=req.session_id,
                    max_context_tokens=req.max_context_tokens,
                    on_event=emit,
                    keep_turns=req.keep_turns,
                    should_stop=stopped.is_set,
                )
                emit({"type": "answer", **result.to_dict()})
                logger.info("ask: %s", result.stats.footer())
            except AskCancelled:
                logger.info("ask stopped by the client")
            except Exception as e:  # noqa: BLE001 — reported in the stream
                logger.warning("ask failed: %s", e)
                emit({"type": "error", "message": str(e)})
            finally:
                emit({"type": "end"})

        threading.Thread(target=work, name="codewiki-ask", daemon=True).start()

        async def stream():
            try:
                while True:
                    event = await queue.get()
                    if event["type"] == "end":
                        break
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            finally:
                # Also runs when the browser disconnects (Stop, closed tab):
                # the engine then stops before its next LLM call.
                stopped.set()

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.get("/{path:path}")
    def static(path: str):
        rel = path.strip("/") or "index.html"
        if rel.startswith(_HIDDEN_PREFIXES) or "/." in rel:
            raise HTTPException(404)
        full = os.path.realpath(os.path.join(docs_dir, rel))
        if not full.startswith(os.path.realpath(docs_dir) + os.sep) or not os.path.isfile(full):
            raise HTTPException(404)
        return FileResponse(full)

    return app


def serve(
    docs_dir: str | None,
    host: str,
    port: int,
    extra_origins: Iterable[str] = (),
    model: str | None = None,
    log_questions: bool = True,
) -> None:
    import uvicorn

    docs_dir = resolve_docs_dir(docs_dir)
    origins = default_origins(host, port, docs_dir) | {
        o for o in (origin_of(x) or x for x in extra_origins) if o
    }
    app = create_app(docs_dir, origins, model=model, log_questions=log_questions)
    uvicorn.run(app, host=host, port=port, log_level="warning")
