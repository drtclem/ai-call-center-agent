"""Settings from the process environment. Values are never printed."""

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # repo root (or /app in the image)
DEFAULT_APP_TODAY = date(2026, 10, 16)
_ENVS = frozenset({"development", "production"})
_APP_TODAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Settings:
    env: str
    app_today: date
    db_path: Path  # no default: callers must say where (tests use temp copies, rule 1)
    staff_password: str = ""  # empty = staff pages disabled (D12.5)
    reviewer_password: str = ""  # read-only staff view for reviewers; empty = no such login


def get_settings() -> Settings:
    """Read ENV, APP_TODAY and DB_PATH. Does not cache and does not print values."""
    raw_env = os.environ.get("ENV", "development")
    if raw_env not in _ENVS:
        raise ValueError("ENV must be development or production")
    raw_today = os.environ.get("APP_TODAY", DEFAULT_APP_TODAY.isoformat())
    raw_db = os.environ.get("DB_PATH", "")
    if not raw_db and raw_env == "production":
        # the default lives on the container's own disk, which every redeploy wipes (R-31)
        raise ValueError("DB_PATH must be set in production (a file on the Railway volume)")
    db_path = Path(raw_db) if raw_db else ROOT / "var/meridian.sqlite"
    return Settings(
        env=raw_env,
        app_today=_parse_app_today(raw_today),
        db_path=db_path,
        staff_password=os.environ.get("STAFF_PASSWORD", ""),
        reviewer_password=os.environ.get("REVIEWER_PASSWORD", ""),
    )


def _parse_app_today(raw: str) -> date:
    if _APP_TODAY_RE.fullmatch(raw) is None:
        raise ValueError("APP_TODAY must be YYYY-MM-DD")
    year_s, month_s, day_s = raw.split("-")
    try:
        return date(int(year_s), int(month_s), int(day_s))
    except ValueError as exc:
        raise ValueError("APP_TODAY must be a real calendar date") from exc
