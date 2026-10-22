"""The only source of business dates and times (architecture rule 2, D10)."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from meridian.config import get_settings

CENTRAL = ZoneInfo("America/Chicago")


def today() -> date:
    """Return the configured APP_TODAY. Reads settings on every call."""
    return get_settings().app_today


def now() -> datetime:
    """APP_TODAY at the real Central time of day (D10: the date is frozen, the clock isn't).

    Used for the 24-hour late-change rule and timestamps. Naive, in Central time.
    """
    return datetime.combine(today(), datetime.now(CENTRAL).time().replace(microsecond=0))


def ops_date() -> date:
    """The real date in Central time, for operations (spend cap, rate limits; D14), never for
    business logic. Business dates always use `today()`."""
    return datetime.now(CENTRAL).date()


def ops_now() -> datetime:
    """Real current time (UTC, aware), for rate-limit windows only."""
    return datetime.now(UTC)
