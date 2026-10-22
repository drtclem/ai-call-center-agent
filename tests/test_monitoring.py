"""T14 monitoring, staff dashboard and demo actions (D15, D17; R-28, R-29, R-37, R-38)."""

import base64
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_fakes import INDEX, FakeLLM, r, route
from meridian import clock
from meridian.agent.graph import Agent
from meridian.config import Settings
from meridian.db import connect
from meridian.monitoring import metrics
from meridian.web.app import COOKIE, create_app

HX = {"HX-Request": "true"}
SAME = {"Sec-Fetch-Site": "same-origin"}
PW = "correct horse battery"


def _auth(pw: str = PW) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"staff:{pw}".encode()).decode()}


@pytest.fixture
def web(db: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, FakeLLM, Path]]:
    monkeypatch.setenv("APP_TODAY", "2026-10-16")
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 16, 10, 0))
    llm = FakeLLM()
    settings = Settings(env="production", app_today=clock.today(), db_path=db, staff_password=PW)
    app = create_app(settings, lambda: Agent(lambda: connect(db), llm, lambda q: INDEX.search(q)))  # type: ignore[arg-type]
    with TestClient(app, base_url="https://testserver") as client:
        yield client, llm, db
    assert not llm.steps


def _say(client: TestClient, text: str) -> str:
    return client.post("/chat", data={"message": text}, headers=HX).text


def test_staff_pages_need_the_password(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, _, _ = web
    assert client.get("/staff").status_code == 401
    assert client.get("/staff", headers=_auth("wrong")).status_code == 401
    assert client.post("/staff/delete-logs", headers=_auth("wrong")).status_code == 401
    ok = client.get("/staff", headers=_auth())
    assert ok.status_code == 200 and ok.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("pw", ["", "short"])
def test_staff_pages_are_off_without_a_long_password(db: Path, pw: str) -> None:
    settings = Settings(env="production", app_today=clock.today(), db_path=db, staff_password=pw)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert client.get("/staff", headers=_auth(pw)).status_code == 503


def test_staff_actions_need_proof_they_came_from_our_page(
    web: tuple[TestClient, FakeLLM, Path],
) -> None:
    client, _, _ = web
    for headers in ({}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
        resp = client.post("/staff/delete-logs", headers=_auth() | headers)
        assert resp.status_code == 403, headers
    ok = client.post("/staff/delete-logs", headers=_auth() | SAME, follow_redirects=False)
    assert ok.status_code == 303


def test_a_turn_is_recorded_and_shown_to_staff_without_reaching_the_logs(
    web: tuple[TestClient, FakeLLM, Path], caplog: pytest.LogCaptureFixture
) -> None:
    client, llm, db = web
    secret = "my number is 512-555-0101 and email linda@example.com"
    llm.add(route("kb"), r("Thanks for reaching out."))
    with caplog.at_level(logging.DEBUG):
        _say(client, secret)
    assert "512-555-0101" not in caplog.text and "linda@example.com" not in caplog.text
    with connect(db) as c:
        conv_id, outcome, turns = c.execute(
            "SELECT id, outcome, turns FROM conversations"
        ).fetchone()
        roles = [row[0] for row in c.execute("SELECT role FROM turn_events ORDER BY seq")]
    assert (outcome, turns, roles) == ("contained", 1, ["user", "assistant"])
    page = client.get(f"/staff/conversations/{conv_id}", headers=_auth()).text
    assert "512-555-0101" in page and "Thanks for reaching out." in page  # staff see the full text
    dash = client.get("/staff", headers=_auth()).text
    assert f"/staff/conversations/{conv_id}" in dash
    assert client.get("/staff/conversations/nope", headers=_auth()).status_code == 404


def test_abandoned_is_never_contained() -> None:
    now = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
    stale = (now - timedelta(minutes=31)).isoformat()
    fresh = (now - timedelta(minutes=5)).isoformat()
    assert metrics.final_outcome("in_progress", stale, now) == "abandoned"
    assert metrics.final_outcome("in_progress", fresh, now) == "in_progress"
    assert metrics.final_outcome("contained", stale, now) == "contained"


def _seed(db: Path, now: datetime) -> None:
    rows = [  # (minutes ago, outcome, reason, rating, latency)
        (10, "contained", None, 1, 1200),
        (20, "contained", None, -1, 3000),
        (30, "contained_fee_followup", None, None, 2000),
        (40, "handed_off", "safety", None, 900),
        (50, "in_progress", "waiting_on_caller", None, 1500),  # 50 min idle: abandoned
        (2, "in_progress", "waiting_on_caller", None, 800),  # still going: not finished
        (60 * 24 * 3, "contained", None, None, 1000),  # three days ago
    ]
    with connect(db) as c:
        for i, (ago, outcome, reason, rating, latency) in enumerate(rows):
            at = (now - timedelta(minutes=ago)).isoformat()
            c.execute(
                "INSERT INTO conversations (id, started_at, ended_at, visitor_key, outcome, "
                "outcome_reason, rating, turns) VALUES (?, ?, ?, 'v', ?, ?, ?, 1)",
                (f"c{i}", at, at, outcome, reason, rating),
            )
            c.execute(
                "INSERT INTO turn_events (conversation_id, seq, role, text, latency_ms) "
                "VALUES (?, 1, 'assistant', 'x', ?)",
                (f"c{i}", latency),
            )


def test_dashboard_numbers_match_a_seeded_day(db: Path) -> None:
    now = datetime(2026, 10, 19, 20, 0, tzinfo=UTC)  # 3pm Central
    _seed(db, now)
    with connect(db) as c:
        o = metrics.overview(c, now)
    t = o.today
    assert (t.conversations, t.finished, t.contained, t.fee_followup) == (6, 5, 3, 1)
    assert (t.handed_off, t.abandoned, t.thumbs_down, t.contained_unhappy) == (1, 1, 1, 1)
    assert t.containment == pytest.approx(3 / 5)
    assert t.handoff_reasons == {"safety": 1}
    assert (o.p50_ms, o.p95_ms) == (1500, 3000)
    assert sum(d.conversations for d in o.trend) == 7 and len(o.trend) == 14


def test_queues_mask_the_caller(db: Path) -> None:
    with connect(db) as c:
        c.execute(
            "INSERT INTO conversations (id, started_at, visitor_key) "
            "VALUES ('c1', '2026-10-19', 'v')"
        )
        c.execute(
            "INSERT INTO handoff_tickets (conversation_id, reason, urgency, customer_id, summary, "
            "created_at) VALUES ('c1', 'billing', 'normal', 'MHC-00901', 'Billing question', 'x')"
        )
        (q,) = metrics.handoff_queue(c)
    assert q["caller"] == "W. Halvorsen 512-***-0191"
    assert metrics.mask_text("(512) 555-0101, x@y.co") == "512-***-0101, [email]"


def test_reset_demo_restores_bookings_and_ends_chats(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, llm, db = web
    llm.add(
        route("scheduling"),
        r("", ("verify_identity", {"name": "Linda Foster", "phone": "512-555-0101"})),
        r("", ("cancel_visit", {"appointment_id": 159})),
    )
    _say(client, "Linda Foster 512-555-0101, cancel my visit")
    llm.add(route("scheduling"), r("", ("cancel_visit", {"appointment_id": 159})))
    _say(client, "Yes")
    sid = client.cookies.get(COOKIE)
    status = "SELECT status FROM appointments WHERE appointment_id = 159"
    with connect(db) as c:
        assert c.execute(status).fetchone()[0] == "cancelled"
    resp = client.post("/staff/reset-demo", headers=_auth() | SAME, follow_redirects=False)
    assert resp.status_code == 303
    with connect(db) as c:
        assert c.execute(status).fetchone()[0] == "scheduled"
        assert c.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1  # logs kept
        assert c.execute(
            "SELECT count(*) FROM customers WHERE customer_id = 'MHC-00901'"
        ).fetchone()[0]
    client.cookies.set(COOKIE, sid or "")
    assert "Thanks" not in client.post("/chat/rate", data={"rating": "up"}, headers=HX).text


def test_delete_logs_removes_conversations(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, llm, db = web
    llm.add(route("kb"), r("hello"))
    _say(client, "hi")
    client.post("/staff/delete-logs", headers=_auth() | SAME)
    with connect(db) as c:
        for table in ("conversations", "turn_events", "handoff_tickets", "fee_reviews"):
            assert c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


def test_waiting_on_the_caller_then_silence_is_abandoned(
    web: tuple[TestClient, FakeLLM, Path],
) -> None:
    client, llm, db = web
    llm.add(route("scheduling"), r("Sure, can I get your name and phone number?"))
    _say(client, "I want to book a repair")
    with connect(db) as c:
        outcome, ended = c.execute("SELECT outcome, ended_at FROM conversations").fetchone()
    assert outcome == "in_progress"
    later = datetime.fromisoformat(ended) + timedelta(minutes=31)
    assert metrics.final_outcome(outcome, ended, later) == "abandoned"


def test_a_closing_question_still_counts_as_contained() -> None:
    from meridian.monitoring.records import _asks_caller  # pyright: ignore[reportPrivateUsage]

    assert not _asks_caller("A tune-up is $129. Is there anything else I can help with?")
    assert _asks_caller("Which day works best for you?")


def test_a_handoff_stays_the_outcome(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, _, db = web
    _say(client, "I smell gas in the kitchen")
    _say(client, "ok thanks")  # the chat is closed; the outcome must not flip to contained
    with connect(db) as c:
        assert c.execute("SELECT outcome FROM conversations").fetchone()[0] == "handed_off"


def test_a_crash_is_failed_and_listed_as_a_problem(
    web: tuple[TestClient, FakeLLM, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from meridian.agent.graph import Agent as RealAgent

    def boom(*_: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(RealAgent, "_supervisor", boom)
    client, _, db = web
    resp = client.post("/chat", data={"message": "hello"}, headers=HX)
    assert resp.status_code == 200
    with connect(db) as c:
        assert c.execute("SELECT outcome FROM conversations").fetchone()[0] == "failed"
        assert metrics.recent_problems(c)


def test_recording_failure_still_returns_the_reply(
    web: tuple[TestClient, FakeLLM, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import meridian.agent.graph as graph

    def broken(*_: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(graph, "record_turn", broken)
    client, llm, _ = web
    llm.add(route("kb"), r("Happy to help!"))
    resp = client.post("/chat", data={"message": "hello"}, headers=HX)
    assert resp.status_code == 200 and "Happy to help!" in resp.text


def test_caller_text_is_escaped_and_masked_for_staff(
    web: tuple[TestClient, FakeLLM, Path],
) -> None:
    client, _, db = web
    _say(client, "<script>alert(1)</script> gas smell, call 512-555-0199 or a@b.com")
    dash = client.get("/staff", headers=_auth()).text
    assert "<script>alert" not in dash and "512-555-0199" not in dash and "a@b.com" not in dash
    with connect(db) as c:
        (cid,) = c.execute("SELECT id FROM conversations").fetchone()
    detail = client.get(f"/staff/conversations/{cid}", headers=_auth()).text
    assert "&lt;script&gt;" in detail and "<script>alert" not in detail


def test_client_ip_check_is_staff_only(web: tuple[TestClient, FakeLLM, Path]) -> None:
    """T17 forwarded-IP check: staff can see which IP the limits key on."""
    client, _, _ = web
    assert client.get("/staff/client-ip").status_code == 401
    got = client.get("/staff/client-ip", headers=_auth() | {"X-Forwarded-For": "9.9.9.9"})
    assert got.json() == {"client_ip": "testclient"}  # no proxy-headers middleware in tests


RPW = "reviewer only looks around"


@pytest.fixture
def both(db: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_TODAY", "2026-10-16")
    settings = Settings(
        env="production",
        app_today=clock.today(),
        db_path=db,
        staff_password=PW,
        reviewer_password=RPW,
    )
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


def test_reviewer_login_sees_everything_and_can_do_nothing(both: TestClient, db: Path) -> None:
    """Reviewer password (README): view the dashboard, never reset, delete or close anything."""
    page = both.get("/staff", headers=_auth(RPW))
    assert page.status_code == 200 and "Reviewer (read-only)" in page.text
    assert "Reset bookings" not in page.text and "Delete all conversation logs" not in page.text
    assert both.get("/staff/flags", headers=_auth(RPW)).status_code == 200
    with connect(db) as c:
        before = c.execute("SELECT count(*) FROM appointments").fetchone()[0]
    for path in (
        "/staff/reset-demo",
        "/staff/delete-logs",
        "/staff/tickets/1/close",
        "/staff/fees/1/close",
        "/staff/flags/1/review",
        "/staff/flags/1/add-test",
    ):
        assert both.post(path, headers=_auth(RPW) | SAME).status_code == 403, path
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM appointments").fetchone()[0] == before
    staff = both.get("/staff", headers=_auth())
    assert "Reset bookings" in staff.text and "Reviewer (read-only)" not in staff.text
    assert "spend of" in staff.text and "spend of" not in page.text  # budget isn't public
    assert both.get("/staff/flags/1/test-case.json", headers=_auth(RPW)).status_code == 403


def test_reviewer_sees_conversations_with_phones_and_emails_masked(db: Path) -> None:
    llm = FakeLLM()
    settings = Settings(
        env="production",
        app_today=clock.today(),
        db_path=db,
        staff_password=PW,
        reviewer_password=RPW,
    )
    app = create_app(settings, lambda: Agent(lambda: connect(db), llm, lambda q: INDEX.search(q)))  # type: ignore[arg-type]
    with TestClient(app, base_url="https://testserver") as client:
        llm.add(route("kb"), r("Thanks, I'll pass that on."))
        _say(client, "Call me at 512-555-0199 or jo@example.com about my bill")
        with connect(db) as c:
            cid = c.execute("SELECT id FROM conversations").fetchone()[0]
        staff = client.get(f"/staff/conversations/{cid}", headers=_auth()).text
        rev = client.get(f"/staff/conversations/{cid}", headers=_auth(RPW)).text
    assert "512-555-0199" in staff and "jo@example.com" in staff
    assert "512-555-0199" not in rev and "jo@example.com" not in rev and "512-***-0199" in rev


@pytest.mark.parametrize("rpw", ["", "short", PW])
def test_reviewer_password_must_be_long_and_different(db: Path, rpw: str) -> None:
    settings = Settings(
        env="production",
        app_today=clock.today(),
        db_path=db,
        staff_password=PW,
        reviewer_password=rpw,
    )
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        page = client.get("/staff", headers=_auth(rpw)) if rpw else None
        if rpw == PW:  # the same password is plain staff, never a weaker role
            assert page is not None and "Reset bookings" in page.text
        elif page is not None:
            assert page.status_code == 401
