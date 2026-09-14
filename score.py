"""Priority scoring.

One stored input (consequence); everything else derived. See §05 of the
proposal. Never surfaced as a number — it decides lane order, nothing more.

    score = base(consequence)
          x 2.0   if someone else is blocked on you
          x urgency   deadline: 1 + 3 / max(days_left, 0.5)
                      no deadline: 1 + age_days / tolerance_days
          x 1.3   if the estimate is <= 15 minutes

Meta opts out of the arithmetic entirely: you set the priority by hand on your
own work, so there is no stale machine guess to compensate for.
"""

from __future__ import annotations

from datetime import date, datetime

BASE = {"breaks": 100.0, "costs": 40.0, "improves": 12.0, "optional": 3.0}
QUICK_MINUTES = 15
QUICK_BONUS = 1.3
BLOCKING_MULTIPLIER = 2.0
MAX_URGENCY = 7.0


def _days_until(due: str | None, now: datetime) -> float | None:
    if not due:
        return None
    try:
        d = date.fromisoformat(due)
    except ValueError:
        return None
    return (datetime(d.year, d.month, d.day) - now).total_seconds() / 86400.0


def urgency(due: str | None, age_days: float, tolerance_days: float | None,
            now: datetime) -> float:
    """Deadlines dominate; otherwise age against the track's tolerance."""
    left = _days_until(due, now)
    if left is not None:
        return min(1.0 + 3.0 / max(left, 0.5), MAX_URGENCY)
    if not tolerance_days:
        return 1.0
    return min(1.0 + max(age_days, 0.0) / tolerance_days, MAX_URGENCY)


def score(*, track: str, consequence: str | None, due: str | None = None,
          age_days: float = 0.0, tolerance_days: float | None = None,
          estimate_min: int | None = None, blocked_on: str | None = None,
          blocking_someone: bool = False, now: datetime | None = None) -> float:
    """Higher sorts nearer the top of its lane. Comparable within a lane only."""
    now = now or datetime.now()
    base = BASE.get(consequence or "", 0.0)

    if track == "meta":
        # Sorts by the priority you set, full stop.
        return base

    value = base
    if blocking_someone:
        value *= BLOCKING_MULTIPLIER
    value *= urgency(due, age_days, tolerance_days, now)
    if estimate_min is not None and estimate_min <= QUICK_MINUTES:
        value *= QUICK_BONUS
    return round(value, 3)
