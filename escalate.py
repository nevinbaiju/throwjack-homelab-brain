"""Missing something is the failure this system exists to catch, so this is
the part that speaks up twice.

Two mechanisms, because tasks and chores are different animals:

  tasks   the brain owns them, so it RE-ARMS the alarm. Escalation is a new
          VALARM at a later time, not a second notification service.

  chores  the brain does NOT own them — iOS runs the recurrence, and touching
          them would fight it. So the brain reads the Upkeep list, notices an
          essential chore is badly overdue, and raises a SEPARATE nudge task.
          The chore itself is never modified.

Escalation adds precision, not volume. After ESCALATION_CAP re-arms the brain
stops: something nagged six times and still not done is information, not an
alarm to keep ringing. Infinite nagging trains you to swipe without reading,
which costs you every future alert too.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import board
import caldav
import policy
import store
import upkeep

ESCALATION_CAP = 4

# How much later each successive re-arm is. Past the end of the list the task
# stops re-arming and waits for the weekly review instead.
BACKOFF = [timedelta(hours=2), timedelta(hours=6), timedelta(days=1), timedelta(days=3)]

DEFAULT_GRACE = {"daily": timedelta(hours=4)}
OTHER_GRACE = timedelta(days=2)


def _parse_ics_dt(value: str) -> datetime | None:
    value = (value or "").strip()
    m = re.match(r"^(\d{8})T(\d{6})(Z?)$", value)
    if not m:
        return None
    stamp = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    if m.group(3) == "Z":
        stamp = stamp.replace(tzinfo=None) + (datetime.now() - datetime.utcnow())
    return stamp


def _due_of(raw_ics: str) -> datetime | None:
    for line in caldav._unfold(raw_ics):
        if line.upper().startswith("DUE"):
            return _parse_ics_dt(line.partition(":")[2])
    return None


def grace_for(chore: dict) -> timedelta:
    explicit = chore.get("escalate_after")
    if explicit:
        m = re.fullmatch(r"(\d+)\s*([hd])", str(explicit).strip().lower())
        if m:
            n = int(m.group(1))
            return timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)
    return DEFAULT_GRACE.get(str(chore.get("repeat", "")).strip().lower(), OTHER_GRACE)


def nudge_text(title: str, overdue: timedelta, level: int) -> str:
    """Getting louder means getting more specific, never more insistent."""
    hours = int(overdue.total_seconds() // 3600)
    if hours < 24:
        late = f"{hours}h ago" if hours else "just now"
    else:
        late = f"{hours // 24}d ago"
    if level <= 1:
        return f"{title} — due {late}"
    if level == 2:
        return f"Still not done: {title}, due {late}"
    return f"{title} has been waiting {late}. Do it, or drop it from upkeep.yaml."


def escalate_tasks(conn, now: datetime | None = None, cfg: dict | None = None) -> dict:
    """Re-arm the alarm on anything whose reminder passed unanswered."""
    now = now or datetime.now()
    cfg = cfg or policy.load_config()
    result = {"rearmed": 0, "capped": 0}

    for row in conn.execute(
            "SELECT * FROM tasks WHERE archived=0 AND status!='completed' "
            "AND caldav_collection IS NOT NULL AND last_alarm_at IS NOT NULL"):
        fired = _iso(row["last_alarm_at"])
        if fired is None or fired > now:
            continue                                   # not due yet
        if row["dismissed_until"] and _iso(row["dismissed_until"]) and _iso(row["dismissed_until"]) > now:
            continue                                   # you swiped it away today
        level = (row["escalations"] or 0)
        if level >= ESCALATION_CAP:
            result["capped"] += 1
            continue

        nxt = now + BACKOFF[min(level, len(BACKOFF) - 1)]
        when = policy.next_open(row["track"], nxt, cfg) or nxt
        conn.execute("UPDATE tasks SET escalations=?, last_alarm_at=? WHERE id=?",
                     (level + 1, when.isoformat(timespec="seconds"), row["id"]))
        fresh = conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
        try:
            board.write_task(conn, fresh, alarm_at=when)
            result["rearmed"] += 1
        except caldav.CalDavError as e:
            print(f"[escalate] {row['id']}: {e}", flush=True)
    return result


def escalate_chores(conn, now: datetime | None = None, cfg: dict | None = None) -> dict:
    """Raise a nudge task for an essential chore that is badly overdue.

    Reads the Upkeep list; never writes to it. The chore keeps its recurrence
    and its own alarm, untouched.
    """
    now = now or datetime.now()
    cfg = cfg or policy.load_config()
    collection = board.LANES["upkeep"][0]
    result = {"raised": 0, "cleared": 0, "watched": 0}

    by_uid = {}
    for chore in upkeep.load():
        try:
            uid, _ = upkeep.build(chore)
        except upkeep.ChoreError:
            continue
        by_uid[uid] = chore

    live = {}
    for item in caldav.list_todos(collection):
        if item["uid"].startswith(upkeep.UID_PREFIX):
            live[item["uid"]] = item

    for uid, chore in by_uid.items():
        if not chore.get("essential"):
            continue
        result["watched"] += 1
        source_key = f"chore:{uid}"
        existing = store.task_by_source(conn, source_key)

        raw = caldav._request("GET", caldav._url(collection, f"{uid}.ics"))[2].decode(
            "utf-8", errors="replace")
        due = _due_of(raw)
        overdue = (now - due) if due else timedelta(0)
        late_enough = due is not None and overdue > grace_for(chore)

        if not late_enough:
            if existing:                    # you did it — the nudge is spent
                conn.execute("UPDATE tasks SET archived=1, updated_at=? WHERE id=?",
                             (store.now_iso(), existing["id"]))
                board.remove_task(conn, existing)
                result["cleared"] += 1
            continue

        level = (existing["escalations"] or 0) + 1 if existing else 1
        if level > ESCALATION_CAP:
            continue
        title = nudge_text(chore["title"], overdue, level)
        when = policy.next_open("upkeep", now, cfg) or now

        if existing:
            conn.execute(
                "UPDATE tasks SET title=?, escalations=?, last_alarm_at=?, updated_at=? WHERE id=?",
                (title, level, when.isoformat(timespec="seconds"), store.now_iso(), existing["id"]))
            task_id = existing["id"]
        else:
            task_id = store.create_task(
                conn, title=title, track="upkeep", lane="upkeep", consequence="breaks",
                estimate_min=15, note=f"recurring chore: {chore['title']}")
            conn.execute(
                "UPDATE tasks SET source_key=?, escalations=?, last_alarm_at=? WHERE id=?",
                (source_key, level, when.isoformat(timespec="seconds"), task_id))
        fresh = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        try:
            board.write_task(conn, fresh, alarm_at=when)
            result["raised"] += 1
        except caldav.CalDavError as e:
            print(f"[escalate] chore {uid}: {e}", flush=True)
    return result


def _iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def run(conn, now: datetime | None = None) -> dict:
    cfg = policy.load_config()
    return {"tasks": escalate_tasks(conn, now, cfg), "chores": escalate_chores(conn, now, cfg)}
