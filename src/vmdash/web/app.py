"""FastAPI app: server-rendered dashboard over the read-only SQLite snapshot."""

import argparse
import base64
import hashlib
import logging
import secrets
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import ValidationError

from vmdash.config import WebSettings, add_common_args, load_settings, settings_errors
from vmdash.db import connect_ro, read_inventory
from vmdash.log import setup_logging
from vmdash.models import Inventory
from vmdash.web import fragments, views
from vmdash.web.queries import EMPTY_INVENTORY, EMPTY_STATUS, SyncStatus, read_status

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"

# Runs before the stylesheet so the saved theme and template toggle apply without a flash.
# The CSP allows exactly this inline script by its hash.
BOOT_JS = ('try{var d=document.documentElement;if(localStorage.getItem("vmdash.theme")==="dark")d.dataset.theme="dark";'
           'if(localStorage.getItem("vmdash.tpl")==="1")d.classList.add("show-tpl")}catch(e){}')
BOOT_JS_HASH = base64.b64encode(hashlib.sha256(BOOT_JS.encode()).digest()).decode()
# style-src needs 'unsafe-inline' for the width of the usage bars (style="width:..%"); every value is escaped.
CSP = (f"default-src 'none'; script-src 'self' 'sha256-{BOOT_JS_HASH}'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
]


class CachedStatic(StaticFiles):
    """Static files are versioned by a hash in the URL, so they can be cached for a year."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


class ResponseHeaders:
    """Outermost ASGI middleware: security headers, `Cache-Control: no-store` unless a response sets its own
    (pages and status.json hold inventory data behind a login), and `Server-Timing: app;dur=<ms>`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = time.perf_counter()

        async def timed_send(message):
            if message["type"] == "http.response.start":
                dur = (time.perf_counter() - start) * 1000
                headers = [*message.get("headers", []), *SECURITY_HEADERS,
                           (b"server-timing", f"app;dur={dur:.1f}".encode())]
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, timed_send)


def _static_versions() -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:8] for p in STATIC_DIR.iterdir() if p.is_file()}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def create_app(settings: WebSettings, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> FastAPI:
    app = FastAPI(title="vmdash", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)
    app.add_middleware(ResponseHeaders)
    app.mount("/static", CachedStatic(directory=STATIC_DIR), name="static")

    versions = _static_versions()
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        # relative, so the page also works behind a reverse proxy under a path prefix (/vmdash/)
        static_url=lambda name: f"static/{name}?v={versions.get(name, '0')}",
        fmt_size=views.fmt_size, fmt_ram=views.fmt_ram, pct=views.pct, host_state=views.host_state, bar=fragments.bar,
        row_html=fragments.row_html,
        GROUPS=views.GROUPS, boot_js=Markup(BOOT_JS), ghz=views.ghz,
        vcenter_url=lambda host: settings.vcenter_ui_url or views.vcenter_url(host),
    )

    security = HTTPBasic(realm="vmdash")
    user = settings.web_user.encode()
    password = settings.web_password.get_secret_value().encode()

    def require_auth(creds: Annotated[HTTPBasicCredentials, Depends(security)]) -> None:
        ok_user = secrets.compare_digest(creds.username.encode(), user)
        ok_pass = secrets.compare_digest(creds.password.encode(), password)
        if not (ok_user and ok_pass):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials",
                                headers={"WWW-Authenticate": 'Basic realm="vmdash"'})

    # A snapshot never changes after it is written, so the parsed inventory is reused until a new one appears.
    # (uid, inventory) is replaced as one tuple so concurrent requests never see a mismatched pair.
    cache: list[tuple[str | None, Inventory]] = [(None, EMPTY_INVENTORY)]
    # Served when a read fails (database locked, I/O error), so open pages keep their data instead of
    # seeing an empty snapshot and reloading into "No VMs yet".
    last_status: list[SyncStatus] = [EMPTY_STATUS]

    def load(with_inventory: bool) -> tuple[Inventory, SyncStatus]:
        conn = None
        try:
            conn = connect_ro(settings.sqlite_path)
            if conn is None:
                return EMPTY_INVENTORY, EMPTY_STATUS
            conn.execute("BEGIN")  # one consistent read of status + inventory
            st = read_status(conn)
            if not with_inventory or st.snapshot_id is None:
                last_status[0] = st
                return EMPTY_INVENTORY, st
            uid, inv = cache[0]
            if uid is None or uid != st.snapshot_uid:
                inv = read_inventory(conn)
                cache[0] = (st.snapshot_uid, inv)
            last_status[0] = st
            return inv, st
        except sqlite3.Error:
            log.exception("could not read %s", settings.sqlite_path)
            st = last_status[0]
            uid, inv = cache[0]
            return (inv if with_inventory and uid == st.snapshot_uid else EMPTY_INVENTORY), st
        finally:
            if conn is not None:
                conn.close()

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def index(request: Request, group: str = "host"):
        if group not in views.GROUPS:
            group = "host"
        inv, st = load(with_inventory=True)
        current = now()
        groups = views.build_groups(inv, group, current)
        return templates.TemplateResponse(request, "index.html", {
            "inv": inv, "status": st, "group": group, "show_host": group != "host",
            "groups": groups, "totals": views.totals_of(groups),
            "badge": views.badge(st, current),
        })

    @app.get("/status.json", dependencies=[Depends(require_auth)])
    def status_json():
        _, st = load(with_inventory=False)
        b = views.badge(st, now())
        run = st.last_run
        return {
            "snapshot_id": st.snapshot_id,
            "snapshot_uid": st.snapshot_uid,
            "last_run": {"status": run.status, "started_at": _iso(run.started_at),
                         "finished_at": _iso(run.finished_at), "error": run.error} if run else None,
            "last_success_at": _iso(st.last_success_at),
            "interval_minutes": st.interval_minutes,
            "badge": {"state": b.state, "line1": b.line1, "line2": b.line2, "banner": b.banner},
        }

    @app.get("/healthz")
    def healthz():
        _, st = load(with_inventory=False)
        # 503 only when the data is too old, so one failed sync does not page anyone; last_run lets
        # monitoring warn on the first failure (failed) or missing tags (partial).
        last_run = st.last_run.status if st.last_run else None
        if st.last_success_at is None:
            return JSONResponse({"ok": False, "last_success_age_s": None, "last_run": last_run}, status_code=503)
        age = int((now() - st.last_success_at).total_seconds())
        ok = age <= 3 * st.interval_minutes * 60
        return JSONResponse({"ok": ok, "last_success_age_s": age, "last_run": last_run},
                            status_code=200 if ok else 503)

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="vmdash-web", description="Serve the vmdash dashboard.")
    add_common_args(parser)
    args = parser.parse_args(argv)
    try:
        settings = load_settings(WebSettings, args)
    except ValidationError as e:
        setup_logging("INFO", None)
        log.error("invalid web settings: %s", settings_errors(e))
        sys.exit(2)
    setup_logging(settings.log_level, settings.log_file)
    log.info("serving %s on %s:%s", settings.sqlite_path, settings.web_host, settings.web_port)
    uvicorn.run(
        create_app(settings), host=settings.web_host, port=settings.web_port, log_config=None, server_header=False,
        ssl_certfile=str(settings.web_ssl_cert) if settings.web_ssl_cert else None,
        ssl_keyfile=str(settings.web_ssl_key) if settings.web_ssl_key else None,
    )
