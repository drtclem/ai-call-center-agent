"""FastAPI application: the public chat page (D12) and health. Docs routes are off in production.

Chat state lives in memory, keyed by a random session cookie.
shortcut: conversations are lost on restart and not shared across processes; one Railway
process (D13) makes this fine for the demo; move state to the DB if we ever scale out.
"""

import hashlib
import logging
import os
import secrets
import threading
import time
import traceback
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from meridian import clock, limits
from meridian.agent.graph import GREETING, Agent
from meridian.agent.state import Conversation, Turn
from meridian.config import Settings, get_settings
from meridian.db import connect, init_db
from meridian.monitoring.records import set_rating
from meridian.scheduling import fee_button, late_change_fee_cents
from meridian.web.staff import add_staff_routes

log = logging.getLogger(__name__)


class NoExceptionText(logging.Filter):
    """Keep the exception type and traceback lines, drop the message: Starlette re-raises after
    our error handler and uvicorn logs it, and a message can quote caller text (D17, T17)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[0] is not None:
            kind, _, tb = record.exc_info
            frames = "".join(traceback.format_tb(tb))
            record.msg = f"{record.getMessage()} [{kind.__name__}]\n{frames}".rstrip()
            record.args, record.exc_info, record.exc_text = (), None, None
        return True


class NoClientIP(logging.Filter):
    """uvicorn's access log line starts with the client address; we keep the path, status and
    timing but not the IP (D17: only a salted hash of the IP is kept)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = ("-", *record.args[1:])
        return True


def _quiet_exceptions() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, NoClientIP) for f in access.filters):
        access.addFilter(NoClientIP())
    for name in ("uvicorn.error", "uvicorn", "fastapi", "starlette"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, NoExceptionText) for f in logger.filters):
            logger.addFilter(NoExceptionText())


HERE = Path(__file__).parent
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",  # no-referrer makes browsers send Origin: null on posts
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
COOKIE = "mc_sid"
MAX_MESSAGE = limits.MAX_MESSAGE_CHARS
MAX_SESSIONS = 5000  # in-memory chats kept; the least recently used are dropped (memory guard)
SESSION_TTL_S = 6 * 60 * 60  # same as the cookie
# 1000 chars form-encoded can reach 12 KB (emoji); check_message gives the friendly refusal
MAX_BODY = 16384
_SALT = os.environ.get("VISITOR_SALT") or secrets.token_hex(16)  # per process unless set


def _real_agent(db: Path) -> Agent:  # pragma: no cover - needs API keys
    from meridian.agent.llm import LLM, embed_texts
    from meridian.kb.search import load_index

    index = load_index(embed_texts)
    return Agent(lambda: connect(db), LLM(), lambda q: index.search(q))


def _anthropic_reachable() -> bool:
    from meridian.agent.llm import provider_reachable  # rule 3: SDKs only in agent/llm.py

    return provider_reachable()


MODEL_CHECK_TTL_S = 300  # a public URL: at most one provider call per 5 minutes


def create_app(
    settings: Settings | None = None,
    agent_factory: Callable[[], Agent] | None = None,
    model_check: Callable[[], bool] = _anthropic_reachable,
) -> FastAPI:
    resolved = get_settings() if settings is None else settings
    production = resolved.env == "production"
    _quiet_exceptions()
    init_db(resolved.db_path)  # first boot copies the dataset DB; later boots keep bookings (D13)
    late_change_fee_cents()  # fail at startup, not mid-call, if the fee schedule lost the row
    app = FastAPI(
        title="Meridian",
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")  # autoescape on for .html (D12.2)
    make_agent = agent_factory or (lambda: _real_agent(resolved.db_path))
    agents: list[Agent] = []  # built on first use, so the app starts without API keys
    agent_lock = threading.Lock()
    chats: dict[str, Conversation] = {}
    turns: dict[str, list[tuple[str, Turn]]] = {}
    locks: dict[str, threading.Lock] = {}  # one turn at a time per conversation

    def agent() -> Agent:
        with agent_lock:
            if not agents:
                agents.append(make_agent())
            return agents[0]

    seen: dict[str, float] = {}  # sid -> last request (monotonic), for the idle timeout
    sessions_lock = threading.Lock()

    def store(sid: str, conv: Conversation) -> threading.Lock:
        """Keep a chat in memory (if it isn't already); the total is capped."""
        with sessions_lock:
            if sid not in chats:
                while len(chats) >= MAX_SESSIONS:
                    oldest = min(chats, key=lambda s: seen.get(s, 0.0))  # least recently used
                    for d in (chats, turns, locks, seen):
                        d.pop(oldest, None)
                chats[sid], turns[sid], locks[sid] = conv, [], threading.Lock()
                seen[sid] = time.monotonic()
            return locks[sid]

    def session(request: Request) -> tuple[str, Conversation]:
        """The caller's chat. Nothing is stored until a message passes the limits (`reply`), so
        empty, refused or cookieless posts can't push real chats out. Idle chats expire."""
        sid = request.cookies.get(COOKIE, "")
        now = time.monotonic()
        with sessions_lock:
            for old in [s for s, t in seen.items() if now - t > SESSION_TTL_S]:
                for d in (chats, turns, locks, seen):
                    d.pop(old, None)
            if sid not in chats:
                sid = secrets.token_urlsafe(24)
                conv = Conversation(secrets.token_hex(8), _visitor(request))
                return sid, conv
            seen[sid] = now
            return sid, chats[sid]

    def page(request: Request, sid: str, conv: Conversation) -> Response:
        today: date = clock.today()
        resp = templates.TemplateResponse(
            request,
            "chat.html",
            {
                "greeting": GREETING,
                "demo_date": f"{today:%a %b} {today.day}, {today:%Y}",
                "turns": turns.get(sid, []),
                "conv": conv,
                "max_message": MAX_MESSAGE,
                "unavailable": _unavailable(),
            },
        )
        return _with_cookie(resp, sid, production)

    def reply(
        request: Request, sid: str, conv: Conversation, text: str, button: bool = False
    ) -> Response:
        """Runs in a worker thread (model calls must not block the server); one turn at a time
        per conversation, so a double click can't run the same change twice."""
        text = text.strip()
        if not text:
            return HTMLResponse("", status_code=204)
        with connect(resolved.db_path) as conn:  # limits first, so a flood never queues on the lock
            verdict = limits.check_message(
                conn, conv.visitor_key, text, conv.turn_no, conv.turn_no == 0
            )
        if verdict.ok and conv.turn_no == 0:
            lock = store(sid, conv)  # a chat is stored once it has a real message
        elif sid in chats:
            lock = locks[sid]
        elif verdict.ok:  # the chat was reset while this request waited
            return HTMLResponse("", status_code=204)
        else:
            lock = threading.Lock()  # a refusal for a chat we never stored: show it, keep nothing
        with lock:
            if button and conv.sched.pending_fee is None:  # re-checked under the lock
                return HTMLResponse("", status_code=204)
            if verdict.ok:
                turn = agent().respond(conv, text, button=button)
                with connect(resolved.db_path) as conn:
                    cost = sum(c.cost_usd_micros for c in turn.model_calls)
                    limits.add_spend(conn, cost, new_conversation=conv.turn_no == 1)
            else:  # a limit: no model call, a clear message (D14)
                turn = Turn(reply=verdict.message, route=f"limit:{verdict.reason}")
            text = text[:MAX_MESSAGE]
            if sid in turns:
                turns[sid].append((text, turn))
        resp = templates.TemplateResponse(
            request, "_turn.html", {"text": text, "turn": turn, "conv": conv}
        )
        return _with_cookie(resp, sid, production)

    @app.exception_handler(Exception)
    async def failed(request: Request, exc: Exception) -> Response:
        """A clear error bubble instead of a silent failure (D12.3). No message text logged."""
        log.error("request failed: %s %s", request.url.path, type(exc).__name__)
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(request, "_error.html", {}, status_code=500)
        return HTMLResponse("Sorry, something went wrong. Please try again.", status_code=500)

    def _unavailable() -> bool:
        with connect(resolved.db_path) as conn:
            return limits.unavailable(conn)

    @app.middleware("http")
    async def harden(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Security headers on every response (D20.12); cross-site form posts are refused."""
        if request.method == "POST" and not _same_origin(request):
            return HTMLResponse("Forbidden", status_code=403)
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers[k] = v
        if production:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return resp

    checked: dict[str, float | bool] = {"at": -MODEL_CHECK_TTL_S - 1.0, "ok": False}
    check_lock = threading.Lock()  # one provider call at a time; others use the last result

    @app.get("/health")
    def health(deep: bool = False) -> Response:
        """Liveness by default. `?deep=1` (the hourly alert, D15.6) also reads the DB and checks
        the model provider, cached for 5 minutes so the public URL can't be used to spam it."""
        if not deep:
            return JSONResponse({"status": "ok"})
        try:
            with closing(connect(resolved.db_path)) as conn:
                conn.execute("SELECT count(*) FROM customers").fetchone()
            db_ok = True
        except Exception as exc:
            log.error("health: DB check failed: %s", type(exc).__name__)
            db_ok = False
        stale = time.monotonic() - float(checked["at"]) > MODEL_CHECK_TTL_S
        if stale and check_lock.acquire(blocking=False):
            try:
                checked["ok"], checked["at"] = model_check(), time.monotonic()
            finally:
                check_lock.release()
        body = {"db": "ok" if db_ok else "failed", "model": "ok" if checked["ok"] else "failed"}
        ok = db_ok and bool(checked["ok"])
        return JSONResponse({"status": "ok" if ok else "failed", **body}, 200 if ok else 503)

    @app.get("/", response_class=HTMLResponse)
    def chat_page(request: Request) -> Response:
        sid, conv = session(request)
        return page(request, sid, conv)

    @app.post("/chat", response_class=HTMLResponse)
    async def chat(request: Request) -> Response:
        sid, conv = session(request)
        form = await _form(request)
        resp = await run_in_threadpool(reply, request, sid, conv, form.get("message", ""))
        return resp if request.headers.get("HX-Request") else _back(sid, production)

    @app.post("/chat/consent", response_class=HTMLResponse)
    async def consent(request: Request) -> Response:
        """The fee-consent button (D10.8): the only path that sends method="button"."""
        sid, conv = session(request)
        if conv.sched.pending_fee is None:
            return HTMLResponse("", status_code=204)
        resp = await run_in_threadpool(reply, request, sid, conv, fee_button(), True)
        return resp if request.headers.get("HX-Request") else _back(sid, production)

    @app.post("/chat/rate", response_class=HTMLResponse)
    async def rate(request: Request) -> Response:
        sid, conv = session(request)
        value = {"up": 1, "down": -1}.get((await _form(request)).get("rating", ""))
        saved = (
            value is not None and bool(turns.get(sid)) and not conv.closed
        )  # contained chats only
        if saved and value is not None:
            with connect(resolved.db_path) as conn:
                set_rating(conn, conv.id, value)
        msg = "Thanks for the feedback." if saved else ""
        if request.headers.get("HX-Request"):
            return HTMLResponse(f'<p class="rated">{msg}</p>')
        return _back(sid, production)

    @app.post("/chat/new")
    def new_chat(request: Request) -> Response:
        sid = request.cookies.get(COOKIE, "")
        with sessions_lock:
            for d in (chats, turns, locks, seen):
                d.pop(sid, None)
        return RedirectResponse("/", 303)

    def end_all_chats() -> None:
        with sessions_lock:
            for d in (chats, turns, locks, seen):
                d.clear()

    add_staff_routes(app, templates, resolved, end_all_chats, _form)
    return app


async def _form(request: Request) -> dict[str, str]:
    """URL-encoded form fields (no multipart dependency); first value wins. Bodies over 16 KB
    or with odd fields are refused before they're read in full."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise HTTPException(413, "Message too large")
    try:
        return {
            k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), max_num_fields=10).items()
        }
    except ValueError:
        raise HTTPException(400, "Bad form") from None


def _visitor(request: Request) -> str:
    """The visitor key for limits (D14): a salted hash of the client IP, so raw IPs aren't stored
    (D17). Behind Railway, uvicorn's --proxy-headers makes client.host the real IP."""
    ip = request.client.host if request.client else "unknown"
    return hashlib.sha256((_SALT + ip).encode()).hexdigest()[:16]


def _with_cookie(resp: Response, sid: str, production: bool) -> Response:
    resp.set_cookie(
        COOKIE, sid, httponly=True, samesite="lax", secure=production, max_age=60 * 60 * 6
    )
    return resp


def _back(sid: str, production: bool) -> Response:
    """No-JavaScript fallback: plain form posts redirect back to the page (keeping the session)."""
    return _with_cookie(RedirectResponse("/#composer", 303), sid, production)


def _same_origin(request: Request) -> bool:
    """Browsers send Origin on POST; if it's there it must be this site (CSRF guard)."""
    origin = request.headers.get("origin")
    if (
        origin is None
        or origin == "null"
        and request.headers.get("sec-fetch-site") == "same-origin"
    ):
        return True
    host = request.headers.get("host", "")
    return origin.split("://", 1)[-1] == host
