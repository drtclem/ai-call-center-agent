"""Staff pages (D12.5, D15.4–5, D16, D17): dashboard, conversation detail, queues, flag review,
demo actions.

Behind HTTP Basic auth with one shared password from STAFF_PASSWORD (D12 known gap); if it isn't
set the pages are off. Cross-site posts are already refused by the app's Origin check.
"""

import base64
import json
import secrets
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from meridian import clock, feedback
from meridian.agent.handoff import Reason
from meridian.config import Settings
from meridian.db import connect, delete_conversation_logs, reset_demo_data
from meridian.monitoring import metrics
from meridian.monitoring.records import close_fee_review


def add_staff_routes(
    app: FastAPI,
    templates: Jinja2Templates,
    settings: Settings,
    on_reset: Callable[[], None],
    read_form: Callable[[Request], Awaitable[dict[str, str]]],
) -> None:
    templates.env.filters["ct"] = _central  # pyright: ignore[reportUnknownMemberType]

    def require_staff(request: Request) -> None:
        if len(settings.staff_password) < MIN_PASSWORD:  # no lockout, so it must be long
            raise HTTPException(503, "Staff pages are off (STAFF_PASSWORD unset or too short).")
        header = request.headers.get("authorization", "")
        role = ""
        if header.lower().startswith("basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:
                pw = ""
            role = _role(pw, settings.staff_password, settings.reviewer_password)
        if not role:
            raise HTTPException(
                401, "Staff only", headers={"WWW-Authenticate": 'Basic realm="Meridian staff"'}
            )
        request.state.role = role
        if request.method not in ("GET", "HEAD") and role != "staff":  # reviewers only look
            raise HTTPException(403, "Read-only reviewer login: actions are for staff.")
        if request.method == "POST" and not _sure_same_site(request):
            raise HTTPException(403, "Cross-site request refused")

    staff = APIRouter(prefix="/staff", dependencies=[Depends(require_staff)])

    @staff.get("", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        with closing(connect(settings.db_path)) as conn:
            if request.state.role == "staff":  # a public reviewer login mustn't drive DB writes
                feedback.flag_abandoned(conn)
            ctx = {
                "o": metrics.overview(conn),
                "open_flags": conn.execute(
                    "SELECT count(*) FROM flags WHERE reviewed_by IS NULL"
                ).fetchone()[0],
                "resolutions": feedback.resolution_counts(conn),
                "codes": feedback.RESOLUTIONS,
                "problems": metrics.recent_problems(conn),
                "tickets": metrics.handoff_queue(conn),
                "fees": metrics.fee_queue(conn),
                "recent": metrics.recent_conversations(conn),
            }
        return _no_store(templates.TemplateResponse(request, "staff/dashboard.html", ctx))

    @staff.get("/client-ip")
    def client_ip(request: Request) -> dict[str, str]:
        """The IP the limits key on, for the T17 forwarded-IP check: send a fake
        X-Forwarded-For and it must not come back. Staff only; it's the caller's own IP."""
        return {"client_ip": request.client.host if request.client else "unknown"}

    @staff.get("/conversations/{conversation_id}", response_class=HTMLResponse)
    def conversation(request: Request, conversation_id: str) -> Response:
        with closing(connect(settings.db_path)) as conn:
            detail = metrics.conversation_detail(conn, conversation_id)
        if detail is None:
            raise HTTPException(404, "No such conversation")
        if request.state.role != "staff":  # the reviewer password is public (D17.3)
            detail = _masked(detail)
        return _no_store(templates.TemplateResponse(request, "staff/conversation.html", detail))

    @staff.post("/tickets/{ticket_id}/close")
    async def ticket_done(request: Request, ticket_id: int) -> Response:
        code = (await read_form(request)).get("resolution", "")
        with closing(connect(settings.db_path)) as conn:
            _or_400(feedback.resolve_ticket, conn, ticket_id, code)
        return RedirectResponse("/staff#tickets", 303)

    @staff.get("/flags", response_class=HTMLResponse)
    def flags(request: Request) -> Response:
        with closing(connect(settings.db_path)) as conn:
            if request.state.role == "staff":  # a public reviewer login mustn't drive DB writes
                feedback.flag_abandoned(conn)
            ctx = {
                "flags": feedback.review_list(conn),
                "tags": feedback.TAGS,
                "reasons": [r.value for r in Reason],
            }
        return _no_store(templates.TemplateResponse(request, "staff/flags.html", ctx))

    @staff.post("/flags/{flag_id}/review")
    async def flag_review(request: Request, flag_id: int) -> Response:
        form = await read_form(request)
        with closing(connect(settings.db_path)) as conn:
            _or_400(
                feedback.review,
                conn,
                flag_id,
                form.get("tag", ""),
                form.get("correct_answer", ""),
                form.get("reviewed_by", ""),
            )
        return RedirectResponse(f"/staff/flags#flag-{flag_id}", 303)

    @staff.post("/flags/{flag_id}/add-test")
    async def flag_add_test(request: Request, flag_id: int) -> Response:
        expected = (await read_form(request)).get("expected", "")
        with closing(connect(settings.db_path)) as conn:
            _or_400(feedback.add_as_test, conn, flag_id, expected)
        return RedirectResponse(f"/staff/flags#flag-{flag_id}", 303)

    @staff.get("/flags/{flag_id}/test-case.json")
    def flag_test_case(request: Request, flag_id: int) -> Response:
        """The stored case, for `python -m evals.conversations.import_feedback` (D16.4)."""
        if request.state.role != "staff":
            raise HTTPException(403, "Read-only reviewer login: test cases are for staff.")
        with closing(connect(settings.db_path)) as conn:
            row = conn.execute("SELECT test_case FROM flags WHERE id = ?", (flag_id,)).fetchone()
        if row is None or row[0] is None:
            raise HTTPException(404, "No test case for this flag")
        name = feedback.case_file_name(json.loads(row[0]))
        return _no_store(
            Response(
                row[0],
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{name}"'},
            )
        )

    @staff.post("/fees/{fee_id}/close")
    def fee_done(fee_id: int) -> Response:
        with closing(connect(settings.db_path)) as conn:
            close_fee_review(conn, fee_id)
        return RedirectResponse("/staff#fees", 303)

    @staff.post("/reset-demo")
    def reset() -> Response:
        with closing(connect(settings.db_path)) as conn:
            reset_demo_data(conn)
        on_reset()
        return RedirectResponse("/staff", 303)

    @staff.post("/delete-logs")
    def delete_logs() -> Response:
        with closing(connect(settings.db_path)) as conn:
            delete_conversation_logs(conn)
        on_reset()
        return RedirectResponse("/staff", 303)

    app.include_router(staff)


MIN_PASSWORD = 16


def _masked(value: Any) -> Any:
    """Phones and emails hidden in every string of a conversation's detail, for reviewers."""
    if isinstance(value, str):
        return metrics.mask_text(value)
    if isinstance(value, dict):
        return {k: _masked(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list | tuple):
        return [_masked(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _role(pw: str, staff: str, reviewer: str) -> str:
    """ "staff", "reviewer" (read-only) or "" for a wrong password. Both compared in constant
    time; the reviewer password works only if set, long enough and not the staff password."""
    is_staff = secrets.compare_digest(pw.encode(), staff.encode())
    is_reviewer = (
        len(reviewer) >= MIN_PASSWORD
        and reviewer != staff
        and secrets.compare_digest(pw.encode(), reviewer.encode())
    )
    return "staff" if is_staff else "reviewer" if is_reviewer else ""


def _or_400[**P](fn: Callable[P, object], *args: P.args, **kwargs: P.kwargs) -> None:
    """Staff input errors (bad tag, unreviewed flag, case already added) as a plain 400."""
    try:
        fn(*args, **kwargs)
    except (ValueError, LookupError) as exc:
        raise HTTPException(400, str(exc)) from None


def _sure_same_site(request: Request) -> bool:
    """Browsers resend Basic auth on cross-site posts, so staff actions need positive proof
    they came from our own page (the app-wide check lets a missing Origin through)."""
    if request.headers.get("sec-fetch-site") == "same-origin":
        return True
    origin = request.headers.get("origin", "")
    return origin.split("://", 1)[-1] == request.headers.get("host", "") != ""


def _no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"  # customer details must not sit in caches
    return resp


def _central(iso: str | None) -> str:
    """Stored UTC timestamps shown in Meridian's time."""
    if not iso:
        return "—"
    try:
        t = datetime.fromisoformat(iso)
        t = t.replace(tzinfo=clock.CENTRAL) if t.tzinfo is None else t.astimezone(clock.CENTRAL)
    except ValueError:
        return iso
    return f"{t:%b} {t.day}, {t:%I:%M %p}".replace(" 0", " ")
