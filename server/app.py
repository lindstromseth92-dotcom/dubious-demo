"""Dubious backend: a small local HTTP API the Chrome extension talks to.

  POST /api/scan                       start a scan of the page text the extension collected
  GET  /api/scan/{scan_id}             poll progress and per-claim results
  GET  /api/scan/{scan_id}/claim/{id}  everything behind one claim's score
  GET  /report/{scan_id}[/{claim_id}]  the human-readable explanation pages
  GET  /health

No CORS headers are sent on purpose: the extension's service worker has host
permission and does not need them, and ordinary web pages should not be able
to start scans on your Gemini key.
"""

import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from dubious.config import settings
from dubious.extract import PageBlock
from dubious.pipeline import ScanManager
from dubious.store import Store

logging.basicConfig(level=os.environ.get("DUBIOUS_LOG", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "httpx2", "httpcore", "google_genai", "google_genai.models"):
    logging.getLogger(noisy).setLevel(logging.WARNING)  # one line per outbound request is too much
log = logging.getLogger("dubious.server")

STATIC = Path(__file__).parent / "static"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_TOTAL_CHARS = 80_000

manager: ScanManager | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global manager
    _ = settings.api_key  # fail fast with a clear message if the key is missing
    manager = ScanManager(Store(settings.db_path))
    log.info("Dubious ready: model=%s threshold=%s db=%s", settings.model, settings.threshold, settings.db_path)
    yield
    await manager.close()


app = FastAPI(title="Dubious", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1", "testserver", *filter(None, os.environ.get("DUBIOUS_ALLOWED_HOSTS", "").split(","))],
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/report"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
    return response


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _scrub(value: object) -> object:
    """Replace half-emoji (lone UTF-16 surrogates) one for one, anywhere in the request body.

    A browser can produce them by cutting a string through an emoji. They are not
    valid text, and left alone they make the request fail in a way that cannot
    even be reported back.
    """
    if isinstance(value, str):
        return _LONE_SURROGATE.sub("\ufffd", value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


class ScanRequest(BaseModel):
    url: str = Field(max_length=4000)
    title: str = Field(default="", max_length=1000)
    truncated: bool = False
    threshold: int | None = Field(default=None, ge=0, le=100)
    blocks: list[PageBlock] = Field(min_length=1, max_length=400)

    @model_validator(mode="before")
    @classmethod
    def _clean(cls, data: object) -> object:
        return _scrub(data)


def _manager() -> ScanManager:
    if manager is None:
        raise HTTPException(503, "Backend is still starting")
    return manager


def _check_id(value: str) -> str:
    if not ID_RE.match(value):
        raise HTTPException(404, "Not found")
    return value


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@app.get("/health")
async def health():
    return {"ok": True, "model": settings.model, "threshold": settings.threshold}


@app.post("/api/scan", status_code=202)
async def start_scan(req: ScanRequest):
    if sum(len(b.text) for b in req.blocks) > MAX_TOTAL_CHARS:
        raise HTTPException(413, "Page is too long to scan in one go")
    if not req.url.startswith(("http://", "https://", "file://")):
        raise HTTPException(422, "Only web pages can be scanned")
    scan_id = await _manager().start(req.url, req.title, req.blocks, req.truncated, req.threshold)
    return {"scan_id": scan_id}


Threshold = Annotated[int | None, Query(ge=0, le=100, description="flagging threshold to apply instead of the scan's own")]


@app.get("/api/scan/{scan_id}")
async def get_scan(scan_id: str, request: Request, t: Threshold = None):
    view = await _manager().summary(_check_id(scan_id), _base(request), t)
    if view is None:
        raise HTTPException(404, "Unknown scan")
    return view


@app.get("/api/scan/{scan_id}/claim/{claim_id}")
async def get_claim(scan_id: str, claim_id: str, request: Request, t: Threshold = None):
    view = await _manager().detail(_check_id(scan_id), _check_id(claim_id), _base(request), t)
    if view is None:
        raise HTTPException(404, "Unknown claim")
    return view


@app.get("/report/{scan_id}")
@app.get("/report/{scan_id}/{claim_id}")
async def report(scan_id: str, claim_id: str | None = None):
    _check_id(scan_id)
    if claim_id is not None:
        _check_id(claim_id)
    return FileResponse(STATIC / "report.html", media_type="text/html")


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal error. See the backend log."})
