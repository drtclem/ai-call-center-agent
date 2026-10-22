"""T13 public-URL limits and web hardening (D14, D20.12; R-32, R-33, R-34)."""

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_fakes import INDEX, FakeLLM, r, route
from meridian import clock, limits
from meridian.agent.graph import Agent
from meridian.agent.tools import KB_TOOLS, SCHEDULING_TOOLS
from meridian.config import Settings
from meridian.db import connect
from meridian.web.app import create_app

HX = {"HX-Request": "true"}


@pytest.fixture
def web(db: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, FakeLLM, Path]]:
    monkeypatch.setenv("APP_TODAY", "2026-10-16")
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 16, 10, 0))
    llm = FakeLLM()
    settings = Settings(env="production", app_today=clock.today(), db_path=db)
    app = create_app(settings, lambda: Agent(lambda: connect(db), llm, lambda q: INDEX.search(q)))  # type: ignore[arg-type]
    with TestClient(app, base_url="https://testserver") as client:
        yield client, llm, db
    assert not llm.steps


def _hits(db: Path) -> int:
    with connect(db) as c:
        row = c.execute("SELECT limit_hits FROM usage_daily").fetchone()
    return int(row[0]) if row else 0


def _say(client: TestClient, text: str) -> str:
    return client.post("/chat", data={"message": text}, headers=HX).text


def test_long_message_is_refused_without_a_model_call(
    web: tuple[TestClient, FakeLLM, Path],
) -> None:
    client, _, db = web
    assert "too long" in _say(client, "x" * (limits.MAX_MESSAGE_CHARS + 1))
    assert _hits(db) == 1


def test_messages_per_minute(
    web: tuple[TestClient, FakeLLM, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, llm, db = web
    monkeypatch.setattr(limits, "PER_MINUTE", 2)
    llm.add(route("kb"), r("one"), route("kb"), r("two"))
    _say(client, "hi")
    _say(client, "hi again")
    assert "quickly" in _say(client, "and again")
    assert "quickly" in _say(client, "and again!")
    assert _hits(db) == 2  # every refusal is counted, but only one event row per minute (disk)
    with connect(db) as c:
        assert (
            c.execute(
                "SELECT count(*) FROM rate_events WHERE kind = 'limit:messages_per_minute'"
            ).fetchone()[0]
            == 1
        )


def test_conversations_per_day_count_across_cookies(
    web: tuple[TestClient, FakeLLM, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, llm, _ = web
    monkeypatch.setattr(limits, "CONVERSATIONS_PER_DAY", 1)
    llm.add(route("kb"), r("hello"))
    _say(client, "first chat")
    client.cookies.clear()  # a "new visitor" from the same IP
    assert "new chats" in _say(client, "second chat")


def test_turns_per_conversation(
    web: tuple[TestClient, FakeLLM, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, llm, _ = web
    monkeypatch.setattr(limits, "TURNS_PER_CONVERSATION", 1)
    llm.add(route("kb"), r("hello"))
    _say(client, "first")
    assert "length limit" in _say(client, "second")


def test_spend_cap_makes_the_assistant_unavailable(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, _, db = web
    with connect(db) as c:
        limits.add_spend(c, limits.DAILY_SPEND_CAP_USD_MICROS)
    assert "unavailable" in _say(client, "hello")
    assert "unavailable right now" in client.get("/").text


def test_spend_is_recorded_from_model_usage(web: tuple[TestClient, FakeLLM, Path]) -> None:
    from meridian.agent.llm import Reply, Usage

    client, llm, db = web
    llm.add(
        route("kb"),
        Reply("hi", (), "fake", Usage(), 5000),
    )
    _say(client, "hello")
    with connect(db) as c:
        assert c.execute("SELECT cost_usd_micros, conversations FROM usage_daily").fetchone()[
            :
        ] == (5000, 1)


def test_identity_failures_are_capped_per_ip_across_conversations(
    web: tuple[TestClient, FakeLLM, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, llm, db = web
    monkeypatch.setattr(limits, "IDENTITY_FAILS_PER_DAY", 2)
    for phone in ("512-000-0000", "512-000-0001"):
        llm.add(
            route("scheduling"),
            r("", ("verify_identity", {"name": "Linda Foster", "phone": phone})),
            r("That doesn't match."),
        )
        _say(client, f"Linda Foster {phone}")
        client.cookies.clear()  # each guess from a "fresh" chat
    llm.add(
        route("scheduling"),
        r("", ("verify_identity", {"name": "Linda Foster", "phone": "512-555-0101"})),
    )
    html = _say(
        client, "Linda Foster 512-555-0101"
    )  # the right answer, but this IP is blocked today
    assert "verify the account" in html
    with connect(db) as c:
        assert (
            c.execute("SELECT reason FROM handoff_tickets").fetchone()[0] == "identity_not_verified"
        )
        assert c.execute(
            "SELECT count(*) FROM rate_events WHERE kind = 'limit:identity_cap'"
        ).fetchone()[0]


def test_bookings_per_day(
    web: tuple[TestClient, FakeLLM, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, llm, db = web
    monkeypatch.setattr(limits, "BOOKINGS_PER_DAY", 0)
    llm.add(
        route("scheduling"),
        r("", ("verify_identity", {"name": "Linda Foster", "phone": "512-555-0101"})),
        r("", ("find_slots", {"service_type": "hvac_repair"})),
        r("Options 1-3?"),
    )
    _say(client, "Linda Foster 512-555-0101, book a repair")
    llm.add(route("scheduling"), r("", ("book_slot", {"option": 1, "issue": "AC"})))
    _say(client, "The first one works.")
    with connect(db) as c:
        assert (
            c.execute("SELECT reason FROM handoff_tickets").fetchone()[0]
            == "not_allowed_for_the_assistant"
        )
        assert (
            c.execute(
                "SELECT count(*) FROM appointments WHERE customer_id = 'MHC-10001'"
            ).fetchone()[0]
            == 1
        )
        assert c.execute(
            "SELECT count(*) FROM rate_events WHERE kind = 'limit:bookings_per_day'"
        ).fetchone()[0]


def test_security_headers_and_cookie_flags(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, _, _ = web
    resp = client.get("/")
    h = resp.headers
    assert (
        "default-src 'self'" in h["content-security-policy"]
        and "'unsafe-inline'" not in h["content-security-policy"]
    )
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert "max-age" in h["strict-transport-security"]
    cookie = h["set-cookie"].lower()
    assert all(flag in cookie for flag in ("httponly", "secure", "samesite=lax", "max-age"))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_the_agent_has_no_way_to_fetch_urls() -> None:
    names = {t.name for t in [*KB_TOOLS, *SCHEDULING_TOOLS]}
    assert not {n for n in names if any(w in n for w in ("fetch", "url", "http", "browse", "web"))}


def test_cross_site_posts_are_refused(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, _, _ = web
    resp = client.post(
        "/chat", data={"message": "hi"}, headers={**HX, "Origin": "https://evil.example"}
    )
    assert resp.status_code == 403


def test_oversized_or_malformed_forms_are_refused(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, _, _ = web
    assert (
        client.post(
            "/chat",
            content=b"message=" + b"x" * 17000,
            headers={**HX, "Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 413
    )
    many = "&".join(f"f{i}=x" for i in range(12))
    assert (
        client.post(
            "/chat",
            content=many.encode(),
            headers={**HX, "Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 400
    )


def test_page_views_do_not_create_sessions_and_raw_ips_are_not_stored(
    web: tuple[TestClient, FakeLLM, Path],
) -> None:
    client, llm, db = web
    for _ in range(5):
        client.cookies.clear()
        client.get("/")
    llm.add(route("kb"), r("hi"))
    client.post("/chat", data={"message": "hello"}, headers=HX)
    with connect(db) as c:
        keys = [row[0] for row in c.execute("SELECT visitor_key FROM conversations")]
    assert len(keys) == 1 and keys[0] not in ("testclient", "127.0.0.1") and len(keys[0]) == 16


def test_cookieless_posts_cannot_evict_a_live_chat(
    web: tuple[TestClient, FakeLLM, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import meridian.web.app as webapp

    client, llm, _ = web
    monkeypatch.setattr(webapp, "MAX_SESSIONS", 2)
    llm.add(route("kb"), r("hello"))
    _say(client, "hi")
    sid = client.cookies.get(webapp.COOKIE)
    assert sid
    for _ in range(5):  # a flood of fresh visitors posting nothing a model would see stores nothing
        client.cookies.clear()
        client.post("/chat/rate", data={"rating": "up"}, headers=HX)
        client.post("/chat/consent", headers=HX)
        client.post("/chat", data={"message": " "}, headers=HX)
        client.post("/chat", data={"message": "x" * 2000}, headers=HX)  # refused by a limit
    client.cookies.clear()
    client.cookies.set(webapp.COOKIE, sid)
    assert "Thanks" in client.post("/chat/rate", data={"rating": "up"}, headers=HX).text


def test_long_non_ascii_message_gets_the_friendly_refusal(
    web: tuple[TestClient, FakeLLM, Path],
) -> None:
    client, _, _ = web
    resp = client.post("/chat", data={"message": "\U0001f600" * 1001}, headers=HX)
    assert resp.status_code == 200 and "too long" in resp.text


def test_origin_null_needs_same_origin_fetch_metadata(
    web: tuple[TestClient, FakeLLM, Path],
) -> None:
    client, _, _ = web
    assert client.get("/").headers["Referrer-Policy"] == "same-origin"
    resp = client.post("/chat/new", headers={"Origin": "null"}, follow_redirects=False)
    assert resp.status_code == 403


def test_the_memory_cap_drops_the_least_recently_used_chat(
    web: tuple[TestClient, FakeLLM, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import meridian.web.app as webapp

    client, llm, _ = web
    monkeypatch.setattr(webapp, "MAX_SESSIONS", 2)
    sids: dict[str, str] = {}
    for name in ("a", "b"):
        client.cookies.clear()
        llm.add(route("kb"), r("hello"))
        _say(client, "hi")
        sids[name] = client.cookies.get(webapp.COOKIE) or ""
    client.cookies.clear()
    client.cookies.set(webapp.COOKIE, sids["a"])
    llm.add(route("kb"), r("again"))
    _say(client, "hi again")  # "a" is now the most recent
    client.cookies.clear()
    llm.add(route("kb"), r("hello"))
    _say(client, "hi from c")  # pushes out "b", not "a"
    for name, kept in (("a", True), ("b", False)):
        client.cookies.clear()
        client.cookies.set(webapp.COOKIE, sids[name])
        said = client.post("/chat/rate", data={"rating": "up"}, headers=HX).text
        assert ("Thanks" in said) is kept
