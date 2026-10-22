"""R-21: today is APP_TODAY from the process env (D10, architecture rules 2 and 17)."""

import dataclasses
import datetime
import pathlib

import pytest

from meridian.clock import today
from meridian.config import Settings, get_settings

DATASET_TODAY = datetime.date(2026, 10, 16)
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_r21_settings_is_frozen_dataclass() -> None:
    settings = Settings(
        env="development", app_today=DATASET_TODAY, db_path=pathlib.Path("unused.sqlite")
    )

    assert dataclasses.is_dataclass(Settings)
    field_names = {field.name for field in dataclasses.fields(Settings)}
    assert {"env", "app_today"} <= field_names
    assert isinstance(settings.env, str)
    assert type(settings.app_today) is datetime.date
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(settings, "env", "production")  # noqa: B010 — proves the dataclass is frozen


def test_r21_unset_env_defaults_to_development_and_dataset_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("APP_TODAY", raising=False)

    settings = get_settings()

    assert settings.env == "development"
    assert settings.app_today == DATASET_TODAY
    assert type(settings.app_today) is datetime.date


def test_r21_set_app_today_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("APP_TODAY", "2026-11-05")

    assert get_settings().app_today == datetime.date(2026, 11, 5)


@pytest.mark.parametrize("raw_env", ["development", "production"])
def test_r21_accepts_development_and_production(
    monkeypatch: pytest.MonkeyPatch,
    raw_env: str,
) -> None:
    monkeypatch.setenv("ENV", raw_env)
    monkeypatch.setenv("APP_TODAY", "2026-10-16")

    assert get_settings().env == raw_env


@pytest.mark.parametrize(
    "raw_env",
    ["prod", "", "Production", "staging"],
    ids=["prod", "empty", "Production", "staging"],
)
def test_r21_invalid_env_raises(
    monkeypatch: pytest.MonkeyPatch,
    raw_env: str,
) -> None:
    monkeypatch.setenv("ENV", raw_env)
    monkeypatch.setenv("APP_TODAY", "2026-10-16")

    with pytest.raises(ValueError):
        get_settings()


@pytest.mark.parametrize(
    "raw_today",
    [
        "not-a-date",
        "10/16/2026",
        "20261016",
        "2026-10-16T00:00:00",
        "",
        "2026-13-01",
        "2026-02-31",
    ],
)
def test_r21_invalid_app_today_raises(
    monkeypatch: pytest.MonkeyPatch,
    raw_today: str,
) -> None:
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("APP_TODAY", raw_today)

    with pytest.raises(ValueError):
        get_settings()


def test_r21_get_settings_reads_process_env_on_each_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("APP_TODAY", "2026-10-16")
    assert get_settings().app_today == DATASET_TODAY

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("APP_TODAY", "2026-10-20")
    updated = get_settings()

    assert updated.env == "production"
    assert updated.app_today == datetime.date(2026, 10, 20)


def test_r21_get_settings_does_not_print_env_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("APP_TODAY", "2026-03-04")
    monkeypatch.setenv("MERIDIAN_SECRET_CANARY", "canary-do-not-print-7f3a")

    settings = get_settings()

    captured = capsys.readouterr()
    leaked = captured.out + captured.err
    assert "2026-03-04" not in leaked
    assert "production" not in leaked
    assert "canary-do-not-print-7f3a" not in leaked
    assert settings.env == "production"
    assert settings.app_today == datetime.date(2026, 3, 4)


def test_r21_clock_today_returns_app_today_not_a_copied_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clock returns the parsed settings date, including when APP_TODAY is unset."""
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("APP_TODAY", raising=False)

    unset_today = today()

    assert unset_today == DATASET_TODAY
    assert unset_today == get_settings().app_today
    assert type(unset_today) is datetime.date

    monkeypatch.setenv("APP_TODAY", "2026-11-05")
    chosen = datetime.date(2026, 11, 5)

    assert today() == chosen
    assert today() == get_settings().app_today


def test_r21_clock_today_rereads_settings_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("APP_TODAY", "2026-10-01")
    assert today() == datetime.date(2026, 10, 1)

    monkeypatch.setenv("APP_TODAY", "2026-11-05")
    assert today() == datetime.date(2026, 11, 5)
    assert today() == get_settings().app_today


def test_r21_env_example_lists_app_today_and_env() -> None:
    """T0 done: .env.example names every variable the clock settings read."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    names: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        names.add(line.split("=", 1)[0].strip())

    assert {"APP_TODAY", "ENV"} <= names
