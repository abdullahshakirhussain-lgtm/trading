"""FastAPI dashboard. Read-only view onto the same SQLite the bot writes.

Protected by HTTP basic auth — the deployed URL is public, so WEB_PASSWORD
must be set or the app refuses to serve anything.
"""
import logging
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .. import config
from . import queries

log = logging.getLogger(__name__)

app = FastAPI(title="crypto co-pilot", docs_url=None, redoc_url=None)
security = HTTPBasic()
INDEX = Path(__file__).parent / "index.html"


def auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    if not config.WEB_PASSWORD:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "WEB_PASSWORD is not set — dashboard disabled for safety.")
    # compare_digest on both fields to avoid timing leaks
    ok_user = secrets.compare_digest(credentials.username, config.WEB_USER)
    ok_pass = secrets.compare_digest(credentials.password, config.WEB_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials",
                            headers={"WWW-Authenticate": "Basic"})
    return credentials.username


@app.get("/healthz")
async def healthz():
    """Unauthenticated so Railway health checks pass. Exposes no data."""
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index(_: str = Depends(auth)):
    return HTMLResponse(INDEX.read_text(encoding="utf-8"))


@app.get("/api/snapshot")
async def snapshot(_: str = Depends(auth)):
    try:
        return JSONResponse(queries.snapshot())
    except Exception as e:
        log.exception("snapshot failed")
        raise HTTPException(500, str(e))
