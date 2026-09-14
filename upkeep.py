"""Recurring chores.

These are deliberately NOT tasks. The brain writes them into the Upkeep
collection and stops there: they never enter the database, never reach the
board, and reconcile skips them because no task claims their uid. iOS owns
the recurrence, the alarm and the completion — tick one and the phone
schedules the next occurrence itself.

That is the whole point. A recurring chore held as a brain task fights
reconcile: you tick it, iOS spawns the next occurrence, and the brain files
the task under Done.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import yaml

import caldav
import board

CHORES_FILE = Path(__file__).with_name("upkeep.yaml")
UID_PREFIX = "u_"          # never t_, so nothing mistakes one for a task

WEEKDAYS = {"monday": ("MO", 0), "tuesday": ("TU", 1), "wednesday": ("WE", 2),
            "thursday": ("TH", 3), "friday": ("FR", 4), "saturday": ("SA", 5),
            "sunday": ("SU", 6)}


class ChoreError(ValueError):
    pass


def weekday_of(chore: dict) -> str | None:
    """Read the weekday, whatever key survived YAML parsing.

    YAML 1.1 treats `on` as a boolean, so `on: friday` parses the KEY as the
    boolean True, not the string "on". `day:` is the documented spelling;
    the other two are caught so a natural `on:` does not silently lose the
    weekday and turn a Friday chore into "any day this week".
    """
    for key in ("day", "on", True):
        value = chore.get(key)
        if value:
            return str(value)
    return None


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]


def parse_repeat(spec: str, weekday: str | None) -> str:
    """-> an RFC 5545 RRULE value."""
    spec = str(spec).strip().lower()

    if spec == "daily":
        return "FREQ=DAILY"
    if spec == "monthly":
        return "FREQ=MONTHLY"
    if spec == "weekly":
        rule = "FREQ=WEEKLY"
        if weekday:
            rule += f";BYDAY={_byday(weekday)}"
        return rule

    m = re.fullmatch(r"every\s+(\d+)\s+(day|days|week|weeks|month|months)", spec)
    if m:
        n, unit = int(m.group(1)), m.group(2).rstrip("s")
        freq = {"day": "DAILY", "week": "WEEKLY", "month": "MONTHLY"}[unit]
        if n < 1:
            raise ChoreError(f"repeat interval must be at least 1, got {n}")
        rule = f"FREQ={freq};INTERVAL={n}"
        if weekday and freq == "WEEKLY":
            rule += f";BYDAY={_byday(weekday)}"
        return rule

    raise ChoreError(f"cannot read repeat {spec!r} — use daily, weekly, monthly, "
                     f"or 'every N days/weeks/months'")


def _byday(weekday: str) -> str:
    key = str(weekday).strip().lower()
    if key not in WEEKDAYS:
        raise ChoreError(f"unknown weekday {weekday!r}")
    return WEEKDAYS[key][0]


def first_occurrence(at: str, weekday: str | None, now: datetime | None = None) -> datetime:
    """The next time this chore is due, from now."""
    now = now or datetime.now()
    try:
        hh, mm = (int(x) for x in str(at).split(":"))
    except ValueError:
        raise ChoreError(f"cannot read time {at!r} — use HH:MM") from None
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if weekday:
        target = WEEKDAYS[str(weekday).strip().lower()][1]
        ahead = (target - start.weekday()) % 7
        if ahead == 0 and start <= now:
            ahead = 7
        start += timedelta(days=ahead)
    elif start <= now:
        start += timedelta(days=1)
    return start


def build(chore: dict) -> tuple[str, str]:
    """-> (uid, ics) for one chore."""
    title = (chore.get("title") or "").strip()
    if not title:
        raise ChoreError("a chore needs a title")
    uid = UID_PREFIX + _slug(title)
    weekday = weekday_of(chore)
    rrule = parse_repeat(chore.get("repeat", "daily"), weekday)
    due = first_occurrence(chore.get("at", "09:00"), weekday)

    ics = caldav.build_vtodo(uid, title, due=due)
    lines = ics.rstrip("\r\n").split("\r\n")
    out = []
    for line in lines:
        if line.startswith("END:VTODO"):
            out.append(f"RRULE:{rrule}")
            if chore.get("alarm", True):
                # RELATIVE trigger, unlike one-off reminders: an absolute one
                # fires once and every later occurrence is silent.
                out += ["BEGIN:VALARM", f"UID:{uid}-alarm", "ACTION:DISPLAY",
                        "TRIGGER;RELATED=END:PT0M",
                        caldav._fold(f"DESCRIPTION:{caldav._esc(title)}"),
                        "END:VALARM"]
        out.append(line)
    return uid, "\r\n".join(out) + "\r\n"


def load(path: Path | None = None) -> list[dict]:
    path = path or CHORES_FILE
    data = yaml.safe_load(path.read_text()) or {}
    chores = data.get("chores")
    if not isinstance(chores, list) or not chores:
        raise ChoreError(f"{path} has no chores")
    return chores


def sync(path: Path | None = None, prune: bool = True) -> dict:
    """Make the Upkeep list match upkeep.yaml. Only touches u_ items."""
    collection = board.LANES["upkeep"][0]
    caldav.ensure_collection(collection, board.LANES["upkeep"][1])

    chores = load(path)
    wanted: dict[str, str] = {}
    for chore in chores:
        uid, ics = build(chore)
        wanted[uid] = ics

    existing = {i["uid"] for i in caldav.list_todos(collection)}
    result = {"written": [], "removed": [], "left_alone": 0}

    for uid, ics in wanted.items():
        caldav.put_todo(collection, uid, ics)
        result["written"].append(uid)

    for uid in existing:
        if not uid.startswith(UID_PREFIX):
            result["left_alone"] += 1        # a task, or something you added
        elif prune and uid not in wanted:
            caldav.delete_todo(collection, uid)
            result["removed"].append(uid)
    return result
