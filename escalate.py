"""Missing something is the failure this system exists to catch, so this is
the part that speaks up twice.

Two mechanisms, because tasks and chores are different animals:

  tasks   the brain owns them, so it RE-ARMS the alarm. Escalation is a new
          VALARM at a later time, not a second notification service.

  chores  the brain owns the recurrence now. An `essential` chore that is badly
          overdue gets SCHEDULED into today with a real time slot, which rings
          because today-lane rows carry alarms.

That second mechanism replaces the nudge cards. A nudge was a separate task
saying "Laundry has been waiting 4h ago. Do it, or drop it from upkeep.yaml." --
volume rather than precision, and it was silently broken three ways: it matched
the same rows as escalate_tasks so the two ladders raced to the cap within two
passes, rescore_and_sync then re-pushed the card with intended_alarm() = None and
stripped the alarm the escalator had just set, and every chore marked essential
was one of the daily reminders that has since been deleted. Doing the thing you
would have done -- putting it on today at a time -- is one mechanism instead of
two, and it is the only one that actually notifies.

Escalation adds precision, not volume. After ESCALATION_CAP re-arms the brain
stops: something nagged six times and still not done is information, not an
alarm to keep ringing. Infinite nagging trains you to swipe without reading,
which costs you every future alert too.
"""

from __future__ import annotations

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

# Where an auto-scheduled chore lands when the chore names a rough time of day.
# Nothing before 10:00: that is when the upkeep notification window opens, so an
# earlier slot would just be held until then anyway.
SLOT_FOR_TIME_OF_DAY = {
    "morning": "10:00", "midday": "12:00", "noon": "12:00",
    "afternoon": "15:00", "evening": "19:00", "night": "21:00", "late": "21:00",
}
DEFAULT_SLOT = "18:00"


def escalate_tasks(conn, now: datetime | None = None, cfg: dict | None = None) -> dict:
    """Re-arm the alarm on anything whose reminder passed unanswered."""
    now = now or policy.local_now()
    cfg = cfg or policy.load_config()
    result = {"rearmed": 0, "capped": 0}

    for row in conn.execute(
            "SELECT * FROM tasks WHERE archived=0 AND status!='completed' "
            "AND caldav_collection IS NOT NULL AND last_alarm_at IS NOT NULL "
            # Chore instances are excluded. The ladder runs to +3d, which would
            # outlive the day whose instance is archived overnight -- leaving an
            # alarm for a card that no longer exists. A chore that was not done
            # today comes back through the due engine tomorrow instead, which is
            # the whole design.
            "AND (source_key IS NULL OR source_key NOT LIKE 'upkeep:%')"):
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


def slot_for(chore: dict) -> str:
    key = str(chore.get("time_of_day") or "").strip().lower()
    return SLOT_FOR_TIME_OF_DAY.get(key, DEFAULT_SLOT)


def escalate_chores(conn, now: datetime | None = None, cfg: dict | None = None) -> dict:
    """Put a badly-overdue essential chore on today, with a time.

    Only `essential` chores. A plain chore that is overdue is not an emergency:
    it keeps being offered at planning time and waits, indefinitely if that is
    what you choose. Nothing here interrupts you for one.
    """
    now = now or policy.local_now()
    cfg = cfg or policy.load_config()
    today = now.date()
    result = {"scheduled": 0, "watched": 0, "already": 0}

    try:
        states = upkeep.all_states(conn, today)
    except Exception as e:                                   # noqa: BLE001
        print(f"[escalate] cannot read chores: {type(e).__name__}: {e}", flush=True)
        return dict(result, error=f"{type(e).__name__}: {e}")

    for chore in states:
        if not chore.get("essential") or chore.get("state") != upkeep.DUE:
            continue
        result["watched"] += 1
        if chore.get("scheduled_today"):
            result["already"] += 1
            continue
        if timedelta(days=chore.get("overdue_days") or 0) <= upkeep.escalation_grace(chore):
            continue

        try:
            task_id = upkeep.materialise(
                conn, chore, slot_for(chore), day=today, automatic=True,
                why=f"overdue {chore['overdue_days']}d, scheduled for you")
        except upkeep.ChoreError as e:
            print(f"[escalate] chore {chore['slug']}: {e}", flush=True)
            continue
        if task_id is None:
            # An instance already exists for today, archived or not. Archived
            # means you dismissed it or the sweep took it; re-creating it here is
            # how "it disappeared overnight" becomes "it came back in ten
            # minutes", which is the behaviour that makes people mute a list.
            result["already"] += 1
            continue

        fresh = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        when = board.intended_alarm(fresh, cfg) or now
        # Clamp forward. policy.next_open searches from `when`, so a slot earlier
        # in the day returns that past time, and a VALARM in the past either
        # fires on sync or never. project.promote does the same thing for the same
        # reason.
        when = max(when, now + timedelta(minutes=1))
        try:
            board.write_task(conn, fresh, alarm_at=when)
            result["scheduled"] += 1
        except caldav.CalDavError as e:
            print(f"[escalate] chore {chore['slug']}: {e}", flush=True)
    return result


def _iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def run(conn, now: datetime | None = None) -> dict:
    cfg = policy.load_config()
    return {"tasks": escalate_tasks(conn, now, cfg), "chores": escalate_chores(conn, now, cfg)}
