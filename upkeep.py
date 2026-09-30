"""Recurring chores: policy in YAML, state in SQLite.

A chore is NOT a standing card. `upkeep.yaml` holds the cadence; the
`upkeep_state` table holds when you last did it. A chore becomes a real task only
for the day it is scheduled, and that instance is archived if the day ends
without it done. The brain owns the recurrence; iOS owns nothing.

That is the reverse of the previous design, which wrote RRULE items into an
Upkeep list and let iOS run them. iOS advances a recurrence from the *previous
due date*, so finishing a fortnightly chore four days late made the next one due
in ten days, not fourteen. Anchoring on what you actually did is the whole point
of this module.

Deliberately imports neither `board` nor `caldav`. The due engine, the state
machine and the overnight sweep are plain functions over YAML, SQLite and dates,
so all of it is testable on the host in under a second instead of needing a
container and a scratch CalDAV server. The one place that must reach the board --
archiving an instance removes its card -- imports it locally.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import yaml

import policy
import store

# Shipped with the image, but edited at runtime -- from the UI and by hand -- so
# the live copy lives on the mounted volume. /app is baked into the image and a
# rebuild would silently discard every change.
PACKAGED_CHORES = Path(__file__).with_name("upkeep.yaml")
CHORES_FILE = store.BRAIN_ROOT / "upkeep.yaml"

SCHEMA_VERSION = 2
SOURCE_PREFIX = "upkeep:"

# A chore instance is one task for one day. This is the identity that everything
# keys on -- the sweep, dedupe, the planner's refusals -- so the slug must never
# contain a colon. Enforced in _check().
def source_key(slug: str, day: date) -> str:
    return f"{SOURCE_PREFIX}{slug}:{day.isoformat()}"


def parse_source_key(value: str | None) -> tuple[str, date] | None:
    """-> (slug, day) for a chore instance, else None."""
    if not value or not value.startswith(SOURCE_PREFIX):
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        return parts[1], date.fromisoformat(parts[2])
    except ValueError:
        return None


WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
WEEKEND = frozenset({5, 6})
WEEKDAY = frozenset({0, 1, 2, 3, 4})

# Every forward search over days is bounded. An unsatisfiable window -- `days: []`,
# or a typo that parses to an empty set -- would otherwise spin forever, and the
# call site is inside /plan/draft, so it would hang a request rather than fail.
WALK_LIMIT = 400

# Slots are 06:00-23:59. Not arbitrary: it puts both daylight-saving edges out of
# reach. A naive 02:30 local does not exist on the spring-forward date and is
# ambiguous on the fall-back one, and every timestamp in this database is naive
# local, so the conversion at write time would be silently wrong twice a year.
SLOT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
SLOT_MIN_HOUR = 6

# A completed instance is kept this long and then archived. The durable record of
# having done the chore is upkeep_state.last_done, not the instance row -- which
# is exactly why last_done is stored rather than derived.
PRUNE_COMPLETED_DAYS = 30

ALLOWED_KEYS = {"id", "title", "every", "window", "days", "time_of_day",
                "est_min", "essential", "escalate_after"}

DUE = "due"
WAITING = "waiting"
NOT_DUE = "not_due"
SNOOZED = "snoozed"

V2_HEADER = """\
# Recurring chores. YOURS to edit -- by hand here, or from the Upkeep tab.
#
# The brain owns the recurrence. A chore is not a standing card: it becomes a
# real task only for the day you schedule it, and that instance disappears
# overnight if you did not do it. Nothing here is ever pushed to your phone until
# it is scheduled into a day.
#
# Due is measured from WHEN YOU LAST DID IT, not from a calendar slot:
#
#     next due = last done + every
#
# so finishing a 14d chore four days late means the next one is due fourteen days
# from then, not ten. A chore you have never done is due the day you add it.
#
#   id:           stable identifier. Keep it fixed -- it is what `last done` is
#                 filed under, so changing it makes the chore look never-done.
#                 Renaming `title` is always safe.
#   title:        what you see on the card.
#   every:        7d | 2w | 1m. Months clamp: the 31st + 1m is the 28th/29th.
#   window:       weekend | weekday -- the only days this chore may land on.
#   days:         [sat, sun] instead, for anything `window` cannot say.
#                 Spell it `days`, never `on` -- YAML reads `on` as a boolean.
#   time_of_day:  free text hint for the planner, e.g. morning / evening.
#   est_min:      how long it takes. Under 15 minutes sorts a little higher.
#   essential:    true lets the brain schedule it for you once it is badly
#                 overdue. Default false: it waits to be picked, forever if need be.
#   escalate_after: how long past due before that happens. Default 2d.
#
# Missing a window defers to the next one: a weekend chore that comes due on
# Saturday and is skipped waits for the following Saturday rather than nagging on
# Tuesday. A chore with no window stays due every day, with the overdue count
# climbing, until you do it.
#
# Not implemented, noted so nobody invents a spelling for it: `skip_cycle`, for
# "forget this one, come back next month" on a chore with no window.

version: 2
"""


class ChoreError(ValueError):
    pass


class SweepOrderError(RuntimeError):
    pass


class ChoreState(NamedTuple):
    """The whole answer for one chore on one day."""
    slug: str
    state: str                  # due | waiting | not_due | snoozed
    next_day: date | None       # when it next becomes actionable
    overdue_days: int           # calendar days since it first became eligible
    missed_windows: int         # eligible windows that closed untouched


# --- policy: the YAML file -------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]


def ensure_chores_file() -> Path:
    """Seed the volume copy from the packaged one, once. Never overwrites."""
    try:
        if not CHORES_FILE.exists() and PACKAGED_CHORES.exists():
            CHORES_FILE.parent.mkdir(parents=True, exist_ok=True)
            CHORES_FILE.write_text(PACKAGED_CHORES.read_text(encoding="utf-8"),
                                   encoding="utf-8")
    except OSError as e:
        print(f"[upkeep] could not seed {CHORES_FILE}: {e}", flush=True)
    return CHORES_FILE if CHORES_FILE.exists() else PACKAGED_CHORES


def migrate_yaml(path: Path) -> dict:
    """Bring a v1 chore file up to v2, in place. Idempotent.

    Necessary because ensure_chores_file() seeds the volume copy once and never
    overwrites it, so editing the packaged file changes nothing on a running
    brain. Without this the v2 engine would read a v1 file, find no `every:`, and
    report every chore as broken.

    v1 chores are NOT converted. Their schedule lived in iOS and their completion
    history is being exported to archive/ by the retire step; carrying the
    cadences across would silently re-create the noise the revamp removes. The
    old file is kept as .bak and the list starts empty.
    """
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError) as e:
        return {"migrated": False, "error": f"{type(e).__name__}: {e}"}
    if not isinstance(data, dict):
        return {"migrated": False, "error": "chore file is not a mapping"}
    if int(data.get("version") or 1) >= SCHEMA_VERSION:
        return {"migrated": False}

    dropped = [str(c.get("title") or "?") for c in (data.get("chores") or [])
               if isinstance(c, dict)]
    backup = path.parent / (path.name + ".bak")
    try:
        backup.write_text(text, encoding="utf-8")
        path.write_text(V2_HEADER + "chores: []\n", encoding="utf-8")
    except OSError as e:
        return {"migrated": False, "error": f"{type(e).__name__}: {e}"}
    print(f"[upkeep] migrated {path} to v2; {len(dropped)} v1 chores dropped, "
          f"old file kept at {backup}", flush=True)
    return {"migrated": True, "dropped": dropped, "backup": str(backup)}


def _check(raw: dict, index: int) -> dict:
    """Validate and normalise one chore. Raises ChoreError naming the problem."""
    if not isinstance(raw, dict):
        raise ChoreError(f"chore #{index + 1} is not a mapping")
    if True in raw or False in raw:
        raise ChoreError(
            f"chore #{index + 1} has a boolean key -- you wrote `on:` or `off:`, "
            f"which YAML 1.1 parses as a boolean, not a string. Use `days:`.")

    title = str(raw.get("title") or "").strip()
    if not title:
        raise ChoreError(f"chore #{index + 1} has no title")

    unknown = set(raw) - ALLOWED_KEYS
    if unknown:
        raise ChoreError(
            f"{title}: unknown key(s) {sorted(unknown)}. Allowed: "
            f"{sorted(ALLOWED_KEYS)}. `at`, `repeat`, `day` and `alarm` were "
            f"retired -- use `every`, `window`/`days` and `time_of_day`.")

    slug = str(raw.get("id") or _slug(title))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise ChoreError(f"{title}: id {slug!r} must be lowercase letters, "
                         f"digits and hyphens (no colons -- it is part of a key)")

    every = raw.get("every")
    if every is None:
        raise ChoreError(f"{title}: needs `every`, e.g. 7d / 2w / 1m")
    try:
        parse_every(str(every))
    except ChoreError as e:
        # Named, because these are read in the Upkeep tab after a hand edit and
        # "cannot read every 'sometimes'" does not say which chore to go and fix.
        raise ChoreError(f"{title}: {e}") from None

    days = _window_days_of(raw, title)
    if days is not None and not days:
        raise ChoreError(f"{title}: window matches no day of the week")

    est = raw.get("est_min")
    if est is not None:
        try:
            est = int(est)
        except (TypeError, ValueError):
            raise ChoreError(f"{title}: est_min must be a whole number of minutes")

    return {"slug": slug, "title": title, "every": str(every),
            "window": raw.get("window"), "days": raw.get("days"),
            "days_set": days, "time_of_day": raw.get("time_of_day"),
            "est_min": est, "essential": bool(raw.get("essential")),
            "escalate_after": raw.get("escalate_after")}


def _window_days_of(raw: dict, title: str) -> frozenset[int] | None:
    """-> the weekdays this chore may land on, or None for "any day"."""
    explicit = raw.get("days")
    if explicit is not None:
        if isinstance(explicit, str):
            explicit = [explicit]
        if not isinstance(explicit, list):
            raise ChoreError(f"{title}: days must be a list, e.g. [sat, sun]")
        out = set()
        for d in explicit:
            key = str(d).strip().lower()
            if key not in WEEKDAYS:
                raise ChoreError(f"{title}: unknown weekday {d!r}")
            out.add(WEEKDAYS[key])
        return frozenset(out)

    win = raw.get("window")
    if win is None:
        return None
    key = str(win).strip().lower()
    if key == "weekend":
        return WEEKEND
    if key == "weekday":
        return WEEKDAY
    if key in ("any", "all"):
        return None
    raise ChoreError(f"{title}: window must be weekend or weekday "
                     f"(or use days: [...]), got {win!r}")


_cache: tuple[float, list[dict], list[str]] | None = None


def load_with_errors(path: Path | None = None) -> tuple[list[dict], list[str]]:
    """-> (valid chores, human-readable problems).

    mtime-cached the same way policy.load_config is, because this is called once
    per mutation while validating a plan and the file barely ever changes.

    Per-chore errors are collected, not raised: one typo must not hide the other
    chores, and the Upkeep tab shows the problems so a bad edit is visible rather
    than silent.
    """
    global _cache
    path = path or ensure_chores_file()
    try:
        mtime = path.stat().st_mtime
    except OSError as e:
        return [], [f"cannot read {path}: {e}"]
    if _cache is not None and _cache[0] == mtime and path == (CHORES_FILE
                                                              if CHORES_FILE.exists()
                                                              else PACKAGED_CHORES):
        return _cache[1], _cache[2]

    migrated = migrate_yaml(path)
    if migrated.get("migrated"):
        mtime = path.stat().st_mtime
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        return [], [f"cannot parse {path}: {e}"]
    if not isinstance(data, dict):
        return [], [f"{path}: top level must be a mapping with a `chores:` list"]

    raw = data.get("chores")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return [], [f"{path}: `chores` must be a list"]

    chores, errors, seen = [], [], set()
    for i, item in enumerate(raw):
        try:
            chore = _check(item, i)
        except ChoreError as e:
            errors.append(str(e))
            continue
        if chore["slug"] in seen:
            errors.append(f"{chore['title']}: duplicate id {chore['slug']!r}")
            continue
        seen.add(chore["slug"])
        chores.append(chore)

    _cache = (mtime, chores, errors)
    return chores, errors


def load(path: Path | None = None) -> list[dict]:
    return load_with_errors(path)[0]


def by_slug(slug: str, path: Path | None = None) -> dict | None:
    for chore in load(path):
        if chore["slug"] == slug:
            return chore
    return None


def save(chores: list[dict], path: Path | None = None) -> None:
    """Rewrite the chore list, keeping the header comments.

    Same trick app.py already uses for the cadence editor: yaml.safe_dump drops
    comments, so the header is carried across verbatim rather than regenerated.
    """
    global _cache
    path = path or ensure_chores_file()
    out = []
    for c in chores:
        item = {"id": c["slug"], "title": c["title"], "every": str(c["every"])}
        if c.get("days"):
            item["days"] = c["days"]
        elif c.get("window"):
            item["window"] = c["window"]
        for key in ("time_of_day", "est_min", "escalate_after"):
            if c.get(key) not in (None, ""):
                item[key] = c[key]
        if c.get("essential"):
            item["essential"] = True
        out.append(item)

    try:
        head = path.read_text(encoding="utf-8").split("\nchores:")[0]
        if "version:" not in head:
            head = V2_HEADER.rstrip("\n")
    except OSError:
        head = V2_HEADER.rstrip("\n")
    body = yaml.safe_dump({"chores": out}, sort_keys=False, allow_unicode=True,
                          default_flow_style=False) if out else "chores: []\n"
    path.write_text(head.rstrip("\n") + "\n\n" + body, encoding="utf-8")
    _cache = None


# --- the due engine: pure ---------------------------------------------------

def parse_every(spec: str) -> tuple[str, int]:
    """'7d' -> ('d', 7). Also 2w, 1m, and the spelled-out forms."""
    m = re.fullmatch(r"(\d+)\s*(d|w|m|day|days|week|weeks|month|months)",
                     str(spec).strip().lower())
    if not m:
        raise ChoreError(f"cannot read every {spec!r} -- use 7d, 2w or 1m")
    n = int(m.group(1))
    if n < 1:
        raise ChoreError(f"every must be at least 1, got {spec!r}")
    return m.group(2)[0], n


def add_every(day: date, spec: str) -> date:
    unit, n = parse_every(spec)
    if unit == "d":
        return day + timedelta(days=n)
    if unit == "w":
        return day + timedelta(days=7 * n)
    total = day.month - 1 + n
    year = day.year + total // 12
    month = total % 12 + 1
    # Clamp: the 31st + 1m is the last day of a short month, not the 1st of the
    # month after. Otherwise a monthly chore drifts forward all year.
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def window_allows(day: date, chore: dict) -> bool:
    days = chore.get("days_set")
    return days is None or day.weekday() in days


def next_eligible(from_day: date, chore: dict, limit: int = WALK_LIMIT) -> date:
    """The first day on or after `from_day` this chore may land on."""
    day = from_day
    for _ in range(limit):
        if window_allows(day, chore):
            return day
        day += timedelta(days=1)
    raise ChoreError(f"{chore.get('title') or chore.get('slug')}: no eligible day "
                     f"within {limit} days of {from_day} -- check window/days")


def _missed_windows(eligible: date, today: date, chore: dict) -> int:
    """Eligible windows that closed strictly before today.

    Counted as eligible -> ineligible transitions, so a Saturday-Sunday pair is
    one window rather than two. Without pinning the definition this way, "how
    many weekends did I skip" is off by one forever.
    """
    if chore.get("days_set") is None or today <= eligible:
        return 0
    count, day = 0, eligible
    for _ in range(WALK_LIMIT):
        if day >= today:
            break
        if window_allows(day, chore) and not window_allows(day + timedelta(days=1), chore):
            count += 1
        day += timedelta(days=1)
    return count


def state_of(chore: dict, *, last_done: date | None, first_seen: date | None,
             snooze_until: date | None, today: date) -> ChoreState:
    """When is this chore next actionable, and how late is it? Pure."""
    if last_done is not None:
        base = add_every(last_done, chore["every"])
    else:
        # Never done. A chore you have just added is due today, not one cadence
        # from now -- otherwise adding "clean the bathroom" tells you to do it in
        # three weeks, which is not why you added it.
        base = first_seen or today

    eligible = next_eligible(base, chore)

    if snooze_until is not None and today <= snooze_until:
        resume = next_eligible(max(eligible, snooze_until + timedelta(days=1)), chore)
        return ChoreState(chore["slug"], SNOOZED, resume,
                          max((today - eligible).days, 0),
                          _missed_windows(eligible, today, chore))

    if today < eligible:
        return ChoreState(chore["slug"], NOT_DUE, eligible, 0, 0)

    overdue = (today - eligible).days
    missed = _missed_windows(eligible, today, chore)
    if window_allows(today, chore):
        return ChoreState(chore["slug"], DUE, today, overdue, missed)
    return ChoreState(chore["slug"], WAITING,
                      next_eligible(today + timedelta(days=1), chore), overdue, missed)


def validate_slot(at: str) -> str:
    """'19:00' -> '19:00'. Raises ChoreError on anything else."""
    text = str(at or "").strip()
    m = SLOT_RE.fullmatch(text)
    if not m:
        raise ChoreError(f"time {at!r} must be HH:MM, 24-hour -- e.g. 19:00")
    if int(m.group(1)) < SLOT_MIN_HOUR:
        raise ChoreError(f"time {text} is before {SLOT_MIN_HOUR:02d}:00; pick a "
                         f"slot in the day you are actually awake for")
    return text


# --- state: the database ---------------------------------------------------

def _as_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def state_row(conn, slug: str):
    return conn.execute("SELECT * FROM upkeep_state WHERE slug=?", (slug,)).fetchone()


def _touch_state(conn, slug: str, **fields) -> None:
    conn.execute(
        "INSERT INTO upkeep_state (slug, updated_at) VALUES (?,?) "
        "ON CONFLICT(slug) DO NOTHING", (slug, store.now_iso()))
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE upkeep_state SET {sets}, updated_at=? WHERE slug=?",
                     (*fields.values(), store.now_iso(), slug))


def ensure_first_seen(conn, slug: str, today: date) -> date:
    """Record when a chore first appeared, and use that as its base.

    A chore has no upkeep_state row until something happens to it, so a
    never-done chore would have nothing to measure `every` from. A `start:` key
    in the YAML would be worse: editing the file could retroactively make
    something four hundred days overdue.
    """
    row = state_row(conn, slug)
    seen = _as_date(row["first_seen"]) if row else None
    if seen is None:
        seen = today
        _touch_state(conn, slug, first_seen=seen.isoformat())
    return seen


def all_states(conn, today: date | None = None, path: Path | None = None) -> list[dict]:
    """Every chore in the YAML, with its state. Iterates the YAML, not the table.

    The other direction would resurrect chores you deleted from the file: nothing
    is deleted from the database, so their upkeep_state rows outlive them.
    """
    today = today or policy.local_today()
    out = []
    for chore in load(path):
        row = state_row(conn, chore["slug"])
        first_seen = ensure_first_seen(conn, chore["slug"], today)
        last_done = _as_date(row["last_done"]) if row else None
        seed = _as_date(row["seed_done"]) if row else None
        if last_done is None or (seed and seed > last_done):
            last_done = seed or last_done
        snooze = _as_date(row["snooze_until"]) if row else None
        try:
            st = state_of(chore, last_done=last_done, first_seen=first_seen,
                          snooze_until=snooze, today=today)
        except ChoreError as e:
            out.append(dict(chore, state="broken", error=str(e), next_due=None,
                            overdue_days=0, missed_windows=0,
                            last_done=last_done.isoformat() if last_done else None,
                            snooze_until=snooze.isoformat() if snooze else None,
                            scheduled_today=None))
            continue
        inst = instance_for(conn, chore["slug"], today)
        out.append(dict(
            chore, state=st.state,
            next_due=st.next_day.isoformat() if st.next_day else None,
            overdue_days=st.overdue_days, missed_windows=st.missed_windows,
            last_done=last_done.isoformat() if last_done else None,
            snooze_until=snooze.isoformat() if snooze else None,
            scheduled_today=({"id": inst["id"], "at": inst["scheduled_at"],
                              "status": inst["status"]} if inst else None)))
    out.sort(key=lambda c: (c["state"] != DUE, -c.get("overdue_days", 0), c["title"]))
    return out


def due_now(conn, today: date | None = None) -> list[dict]:
    return [c for c in all_states(conn, today) if c["state"] == DUE]


# --- instances -------------------------------------------------------------

def instance_for(conn, slug: str, day: date, include_archived: bool = False):
    sql = "SELECT * FROM tasks WHERE source_key=?"
    if not include_archived:
        sql += " AND archived=0"
    return conn.execute(sql, (source_key(slug, day),)).fetchone()


def materialise(conn, chore: dict, at: str, *, day: date, why: str = "",
                today_seq: int | None = None, automatic: bool = False) -> str | None:
    """Create (or re-slot) today's instance of a chore. -> task id, or None.

    One instance per chore per day, archived ones included. Without counting the
    archived ones, "I did not pick it, so it disappeared overnight" turns into
    "it reappeared ten minutes later" -- the sweep archives it, escalation sees
    no live instance, and it is created again.

    `automatic` is escalation. It refuses when any instance exists for the day,
    archived or not, so the brain never re-raises something you dismissed. A user
    schedule un-archives instead: you asked for it, you get it.
    """
    slot = validate_slot(at)
    key = source_key(chore["slug"], day)
    existing = instance_for(conn, chore["slug"], day, include_archived=True)
    scheduled_at = f"{day.isoformat()}T{slot}:00"

    if existing is not None:
        if automatic:
            return None
        conn.execute(
            "UPDATE tasks SET archived=0, lane='today', status='needs_action', "
            "completed_at=NULL, scheduled_at=?, why=?, today_seq=?, "
            # Cleared so the audit re-pushes the card with the new time.
            # write_task only writes last_alarm_at when it is NULL, and the audit
            # compares alarm PRESENCE, not time, so a re-slot is invisible
            # without this.
            "last_alarm_at=NULL, dismissed_until=NULL, escalations=0, "
            "touched_at=?, updated_at=? WHERE id=?",
            (scheduled_at, why or existing["why"] or "", today_seq,
             store.now_iso(), store.now_iso(), existing["id"]))
        return existing["id"]

    task_id = store.create_task(
        conn, title=chore["title"], track="upkeep", lane="today",
        # Without a consequence, score.BASE returns 0.0, board._priority returns
        # 9, and the card sits at the bottom of the list with no "!!!" -- looking
        # like junk on the one day it matters.
        consequence="breaks" if chore.get("essential") else "costs",
        estimate_min=chore.get("est_min"),
        # The date as well as the time: score.urgency only takes its deadline
        # branch when `due` is set, and tolerance_days.upkeep is null, so without
        # it the instance would never age.
        due=day.isoformat(),
        note=f"upkeep: every {chore['every']}")
    conn.execute(
        "UPDATE tasks SET source_key=?, scheduled_at=?, why=?, today_seq=? WHERE id=?",
        (key, scheduled_at, why or f"due every {chore['every']}", today_seq, task_id))
    return task_id


def archive_instance(conn, row, reason: str = "") -> None:
    """Archive an instance and take its card off the phone.

    Removing the card is not optional. board.audit only walks active tasks, so an
    archived row is never audited and its card is never cleaned up -- while
    board.reconcile's main loop reads tasks by id with no archived filter and
    will happily keep flipping it to completed. The result is an immortal card.
    """
    conn.execute("UPDATE tasks SET archived=1, updated_at=? WHERE id=?",
                 (store.now_iso(), row["id"]))
    if reason:
        print(f"[upkeep] archived {row['id']} ({row['source_key']}): {reason}",
              flush=True)
    import board                      # local: keeps this module CalDAV-free
    try:
        board.remove_task(conn, row)
    except Exception as e:
        print(f"[upkeep] could not remove card for {row['id']}: "
              f"{type(e).__name__}: {e}", flush=True)


# --- completion ------------------------------------------------------------

def record_done(conn, slug: str, when: date | str | None = None) -> None:
    """Note that a chore was done. Never moves last_done backwards."""
    day = _as_date(when) or policy.local_today()
    row = state_row(conn, slug)
    current = _as_date(row["last_done"]) if row else None
    if current is not None and current >= day:
        return
    _touch_state(conn, slug, last_done=day.isoformat())


def recompute_last_done(conn, slug: str) -> None:
    """Re-derive last_done after a completion was undone on the phone."""
    row = conn.execute(
        "SELECT MAX(date(completed_at)) d FROM tasks WHERE archived=0 "
        "AND status='completed' AND source_key LIKE ?",
        (f"{SOURCE_PREFIX}{slug}:%",)).fetchone()
    state = state_row(conn, slug)
    seed = _as_date(state["seed_done"]) if state else None
    latest = _as_date(row["d"] if row else None)
    best = max([d for d in (seed, latest) if d is not None], default=None)
    _touch_state(conn, slug, last_done=best.isoformat() if best else None)


def note_completion(conn, row) -> str | None:
    """Hook for every path that completes a task. -> slug, if it was a chore."""
    parsed = parse_source_key(row["source_key"])
    if parsed is None:
        return None
    slug, day = parsed
    record_done(conn, slug, _as_date(row["completed_at"]) or day)
    return slug


def note_reopened(conn, row) -> str | None:
    parsed = parse_source_key(row["source_key"])
    if parsed is None:
        return None
    recompute_last_done(conn, parsed[0])
    return parsed[0]


def snooze(conn, slug: str, until: date | str) -> date:
    day = _as_date(until)
    if day is None:
        raise ChoreError(f"cannot read snooze date {until!r}")
    _touch_state(conn, slug, snooze_until=day.isoformat())
    return day


# --- the overnight sweep ---------------------------------------------------

def sweep_expired(conn, today: date | None = None, *, reconciled: bool) -> dict:
    """Archive yesterday's unfinished instances, and prune old finished ones.

    Keyed on the date inside source_key, NOT on the lane: if you drag an instance
    into Backlog on the phone, a lane-keyed sweep never sees it again and the
    chore is stuck forever.

    `reconciled` must be True, which is how the ordering invariant gets tested
    rather than just commented. If this ran before reconcile, a tick you made at
    23:58 and the phone synced at 00:10 would be erased. The residual race is
    unavoidable and fails safe: a lost tick leaves the chore due, so it asks
    again, rather than being silently assumed done.
    """
    if not reconciled:
        raise SweepOrderError(
            "sweep_expired must run after board.reconcile, or a completion "
            "synced overnight is thrown away")
    today = today or policy.local_today()
    result = {"archived": 0, "pruned": 0}

    for row in conn.execute(
            "SELECT * FROM tasks WHERE archived=0 AND source_key LIKE ?",
            (f"{SOURCE_PREFIX}%",)).fetchall():
        parsed = parse_source_key(row["source_key"])
        if parsed is None:
            continue
        slug, day = parsed
        if row["status"] == "completed":
            if (today - day).days > PRUNE_COMPLETED_DAYS:
                archive_instance(conn, row, "completed, past the keep window")
                result["pruned"] += 1
            continue
        if day < today:
            archive_instance(conn, row, f"not done on {day}")
            result["archived"] += 1
    return result


def escalation_grace(chore: dict) -> timedelta:
    """How long past due before the brain schedules an essential chore for you."""
    explicit = chore.get("escalate_after")
    if explicit:
        m = re.fullmatch(r"(\d+)\s*([hd])", str(explicit).strip().lower())
        if m:
            n = int(m.group(1))
            return timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)
    return timedelta(days=2)
