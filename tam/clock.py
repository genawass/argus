"""Time handling.

Two kinds of time exist in this system and conflating them causes the
off-by-one date bugs that make a task board untrustworthy:

  * instants  -- stored as UTC ISO-8601 with a 'Z' suffix (created_at, ...)
  * calendar dates -- stored as local YYYY-MM-DD (due_date)

A due date is a human day in the user's timezone, not an instant.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .errors import ValidationError

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow():
    """Current instant as a UTC ISO-8601 string."""
    return datetime.now(timezone.utc).strftime(ISO_FMT)


def parse_instant(value):
    """Parse a stored instant back into an aware datetime."""
    return datetime.strptime(value, ISO_FMT).replace(tzinfo=timezone.utc)


def tzinfo_for(name):
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def today(tz_name):
    """Today's calendar date in the configured timezone."""
    return datetime.now(tzinfo_for(tz_name)).date()


def parse_date(value, field="date"):
    """Validate a YYYY-MM-DD calendar date, returning the normalised string."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValidationError(
            f"{field} must be YYYY-MM-DD, got {value!r}", field=field
        )


def expand_date(value, tz_name):
    """Expand the relative date shorthands every adapter accepts.

    `today`, `tomorrow`, `yesterday`, `+3d`, `-2d` -- anything else falls
    through to strict YYYY-MM-DD parsing.
    """
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    base = today(tz_name)
    if raw in ("today", "now"):
        return base.isoformat()
    if raw == "tomorrow":
        return (base + timedelta(days=1)).isoformat()
    if raw == "yesterday":
        return (base - timedelta(days=1)).isoformat()
    if len(raw) > 1 and raw[0] in "+-" and raw.endswith("d") and raw[1:-1].isdigit():
        offset = int(raw[1:-1]) * (1 if raw[0] == "+" else -1)
        return (base + timedelta(days=offset)).isoformat()
    return parse_date(value, "date")


def days_ago(tz_name, n):
    """Instant string for n days before now -- used for staleness filters."""
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime(ISO_FMT)


def shift(d, days):
    """Shift an ISO date string by n days."""
    return (date.fromisoformat(d) + timedelta(days=days)).isoformat()
