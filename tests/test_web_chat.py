"""T12 chat page (D12; R-35, R-16) with a scripted fake model."""

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_fakes import INDEX, FakeLLM, r, route
from meridian import clock
from meridian.agent.graph import Agent
from meridian.config import Settings
from meridian.db import connect
from meridian.scheduling import fee_button
from meridian.web.app import create_app

HX = {"HX-Request": "true"}


@pytest.fixture
def web(db: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, FakeLLM, Path]]:
    monkeypatch.setenv("APP_TODAY", "2026-10-16")
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 16, 10, 0))
    llm = FakeLLM()
    settings = Settings(env="development", app_today=clock.today(), db_path=db)
    app = create_app(settings, lambda: Agent(lambda: connect(db), llm, lambda q: INDEX.search(q)))  # type: ignore[arg-type]
    with TestClient(app) as client:
        yield client, llm, db
    assert not llm.steps


def test_page_shows_greeting_demo_date_and_fictional_data_notice(
    web: tuple[TestClient, FakeLLM, Path],
) -> None:
    client, _, _ = web
    html = client.get("/").text
    assert (
        "virtual assistant" in html
        and "Demo date: Fri Oct 16, 2026" in html
        and "fictional" in html
    )
    cookie = client.get("/").headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_model_output_is_escaped(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, llm, _ = web
    llm.add(route("scheduling"), r('<script>alert("x")</script><img src=x onerror=alert(1)>'))
    html = client.post("/chat", data={"message": "<b>hi</b>"}, headers=HX).text
    assert "<script>alert" not in html and "<img" not in html and "<b>hi" not in html
    assert "&lt;script&gt;" in html and "&lt;b&gt;hi&lt;/b&gt;" in html


def test_handoff_shows_the_ticket_card(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, _, _ = web
    html = client.post("/chat", data={"message": "Can I talk to a real person?"}, headers=HX).text
    assert "Passed to the Meridian team" in html and "asked for a person" in html
    assert 'id="rating"' in html and "Was this helpful?" not in html  # no rating after a handoff


def test_fee_consent_button_only_when_a_notice_is_pending(
    web: tuple[TestClient, FakeLLM, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, llm, db = web
    assert client.post("/chat/consent", headers=HX).status_code == 204  # nothing pending: no-op
    monkeypatch.setenv("APP_TODAY", "2026-10-21")
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 21, 12, 0))
    llm.add(
        route("scheduling"),
        r("", ("verify_identity", {"name": "Linda Foster", "phone": "512-555-0101"})),
        r("", ("cancel_visit", {"appointment_id": 159})),
    )
    client.post(
        "/chat", data={"message": "Linda Foster 512-555-0101, cancel tomorrow's visit"}, headers=HX
    )
    llm.add(route("scheduling"), r("", ("cancel_visit", {"appointment_id": 159})))
    html = client.post("/chat", data={"message": "Yes"}, headers=HX).text
    assert fee_button() in html and 'action="/chat/consent"' in html
    html = client.post("/chat/consent", headers=HX).text
    assert "cancelled" in html
    with connect(db) as c:
        assert c.execute("SELECT consent_method FROM fee_reviews").fetchone()[0] == "button"


def test_rating_is_recorded(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, llm, db = web
    llm.add(route("kb"), r("Happy to help!"))
    html = client.post("/chat", data={"message": "hello"}, headers=HX).text
    assert "Was this helpful?" in html
    assert (
        "Thanks for the feedback"
        in client.post("/chat/rate", data={"rating": "down"}, headers=HX).text
    )
    with connect(db) as c:
        assert c.execute("SELECT rating FROM conversations").fetchone()[0] == -1


def test_works_without_javascript(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, llm, _ = web
    llm.add(route("kb"), r("Happy to help!"))
    resp = client.post("/chat", data={"message": "hello"}, follow_redirects=True)
    assert "Happy to help!" in resp.text and "hello" in resp.text


def test_empty_message_does_nothing(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, _, _ = web
    assert client.post("/chat", data={"message": "   "}, headers=HX).status_code == 204


def test_ticket_card_escapes_the_callers_words(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, _, _ = web
    html = client.post(
        "/chat", data={"message": "gas smell <script>alert(1)</script>"}, headers=HX
    ).text
    assert (
        "Passed to the Meridian team" in html
        and "<script>alert" not in html
        and "&lt;script&gt;" in html
    )


def test_no_rating_without_turns_or_after_a_handoff(web: tuple[TestClient, FakeLLM, Path]) -> None:
    client, _, db = web
    client.post("/chat/rate", data={"rating": "up"}, headers=HX)  # nothing said yet
    client.post("/chat", data={"message": "Can I talk to a real person?"}, headers=HX)
    client.post("/chat/rate", data={"rating": "down"}, headers=HX)  # handed off
    with connect(db) as c:
        assert c.execute("SELECT rating FROM conversations").fetchone()[0] is None


def test_a_server_error_shows_a_message_not_nothing(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        def start(self, *a: object) -> None: ...

        def respond(self, *a: object, **k: object) -> None:
            raise RuntimeError("boom")

    settings = Settings(env="development", app_today=clock.today(), db_path=db)
    app = create_app(settings, lambda: Broken())  # type: ignore[arg-type, return-value]
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/chat", data={"message": "hi"}, headers=HX)
    assert resp.status_code == 500 and "something went wrong" in resp.text


def test_htmx_is_locked_down_and_old_consent_buttons_are_removed(
    web: tuple[TestClient, FakeLLM, Path],
) -> None:
    client, llm, _ = web
    page = client.get("/").text
    assert '"allowEval": false' in page and '"responseHandling"' in page
    llm.add(route("kb"), r("Happy to help!"))
    html = client.post("/chat", data={"message": "hello"}, headers=HX).text
    assert '<div id="consent" hx-swap-oob="delete"></div>' in html and 'id="send"' in html


def test_a_slow_turn_does_not_block_other_requests(db: Path) -> None:
    import threading
    import time

    from meridian.agent.state import Conversation, Turn

    class Slow:
        def respond(self, conv: Conversation, text: str, button: bool = False) -> Turn:
            time.sleep(1.0)
            return Turn(reply="done")

    settings = Settings(env="development", app_today=clock.today(), db_path=db)
    app = create_app(settings, lambda: Slow())  # type: ignore[arg-type, return-value]
    with TestClient(app) as client:
        t = threading.Thread(
            target=lambda: client.post("/chat", data={"message": "hi"}, headers=HX)
        )
        t.start()
        time.sleep(0.2)
        start = time.monotonic()
        assert client.get("/health").status_code == 200
        assert time.monotonic() - start < 0.5
        t.join()


def test_enter_sends_via_a_same_origin_script(web: tuple[TestClient, FakeLLM, Path]) -> None:
    """Live probe: Enter only added a new line. The handler is a static file (CSP: self only)."""
    client = web[0]
    page = client.get("/").text
    assert '<script src="/static/composer.js" defer></script>' in page
    js = client.get("/static/composer.js")
    assert js.status_code == 200 and "requestSubmit" in js.text and "shiftKey" in js.text
