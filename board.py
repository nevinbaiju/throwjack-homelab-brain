"""The brain's task surface on CalDAV. Replaces ha.py.

Lanes are collections, so they show up on the phone as separate Reminders
lists. A task keeps ONE uid for its whole life — moving lanes is a DELETE in
the old collection and a PUT in the new one with the same uid, so identity is
never lost. That is the difference that removes the uid-churn bugs; it is not
that moves stopped happening.

Collections the brain does not own — the shopping lists — are created once so
they appear on your phone, then never read or written again.
"""

from __future__ import annotations

from datetime import datetime

import caldav
import policy
import store

# Lane key -> (collection, display name). Order is the board order.
LANES: dict[str, tuple[str, str]] = {
    "backlog":     ("backlog", "Backlog"),
    "in_progress": ("in-progress", "In Progress"),
    "done":        ("done", "Done"),
    "upkeep":      ("upkeep", "Upkeep"),
}

# The brain has a `waiting` lane (handed off, blocked on someone else) but you
# asked for four lists, so it shares Backlog rather than adding a fifth. The
# track stays `waiting` in the database, so chase intervals still work, and the
# item body says who you are waiting on. Backlog rather than In Progress on
# purpose: blocked work sitting in In Progress inflates how loaded you look.
LANE_ALIASES = {"waiting": "backlog"}


def collection_for(lane: str) -> str:
    return LANES[LANE_ALIASES.get(lane, lane)][0]


def same_lane(db_lane: str, collection_lane: str) -> bool:
    """An aliased lane is not a move. Without this, reconcile would rewrite
    every waiting task to `backlog` the first time it read the board."""
    return LANE_ALIASES.get(db_lane, db_lane) == collection_lane


# Siri and Shortcuts drop here. The brain drains it on every triage pass.
INBOX = ("inbox", "Inbox")

# Yours. Created so they exist on the phone; never touched afterwards.
SHOPPING: list[tuple[str, str]] = [
    ("shop-costco", "Costco"),
    ("shop-fred-meyer", "Fred Meyer"),
    ("shop-safeway", "Safeway"),
    ("shop-indian", "Indian Grocery"),
    ("shop-amazon", "Amazon"),
    ("shop-shein", "Shein"),
]

BRAIN_OWNED = {c for c, _ in LANES.values()} | {INBOX[0]}


def bootstrap() -> dict:
    """Create every collection. Idempotent — safe on every start."""
    made = []
    for collection, name in list(LANES.values()) + [INBOX] + SHOPPING:
        caldav.ensure_collection(collection, name)
        made.append(collection)
    return {"collections": made, "brain_owned": sorted(BRAIN_OWNED),
            "yours": [c for c, _ in SHOPPING]}


def _priority(score: float) -> int:
    """CalDAV PRIORITY is 1 (high) to 9 (low); Reminders renders 1-3 as !!!."""
    if score >= 100:
        return 1
    if score >= 40:
        return 5
    return 9


def write_task(conn, row, *, alarm_at: datetime | None = None) -> None:
    """Create or update a task's item in its lane. Moves it if the lane changed."""
    collection = collection_for(row["lane"])
    body = []
    if row["lane"] == "waiting":
        body.append(f"waiting on {row['blocked_on']}" if row["blocked_on"] else "waiting on someone")
    if row["why"]:
        body.append(row["why"])
    if row["estimate_min"]:
        body.append(f"~{row['estimate_min']} min")
    if row["project"]:
        body.append(f"project: {row['project']}")

    due = None
    if row["due"]:
        try:
            due = datetime.fromisoformat(row["due"] + "T09:00:00").astimezone()
        except ValueError:
            due = None

    ics = caldav.build_vtodo(
        row["id"], row["title"], due=due, alarm_at=alarm_at,
        description="\n".join(body), priority=_priority(row["score"] or 0),
        completed=row["status"] == "completed",
    )

    old = row["caldav_collection"]
    if old and old != collection:
        try:
            caldav.delete_todo(old, row["id"])       # same uid moves across
        except caldav.CalDavError:
            pass

    etag = caldav.put_todo(collection, row["id"], ics)
    conn.execute("UPDATE tasks SET caldav_collection=?, caldav_etag=? WHERE id=?",
                 (collection, etag, row["id"]))
    if alarm_at is not None and row["last_alarm_at"] is None:
        # First alarm for this task. Escalation re-arms from here; it must not
        # overwrite a time escalation itself just set.
        conn.execute("UPDATE tasks SET last_alarm_at=? WHERE id=?",
                     (alarm_at.isoformat(timespec="seconds"), row["id"]))


def remove_task(conn, row) -> None:
    if row["caldav_collection"]:
        try:
            caldav.delete_todo(row["caldav_collection"], row["id"])
        except caldav.CalDavError:
            pass
    conn.execute("UPDATE tasks SET caldav_collection=NULL, caldav_etag=NULL WHERE id=?",
                 (row["id"],))


def alarm_time_for(row, cfg: dict | None = None) -> datetime | None:
    """When this task's reminder should fire, respecting quiet hours.

    Quiet hours used to decide whether to push at fire time. Now they decide
    what trigger we write, so a work item due at 23:00 gets an alarm at 10:00
    the next morning and "held, never dropped" falls out with no queue.
    """
    if not row["due"] or row["status"] == "completed":
        return None
    cfg = cfg or policy.load_config()
    try:
        due = datetime.fromisoformat(row["due"] + "T09:00:00")
    except ValueError:
        return None
    return policy.next_open(row["track"], due, cfg)


def audit(conn) -> list[dict]:
    """Every active task whose card does not match the database.

    Exists because pushes can fail. write_task() raising CalDavError is logged
    and not retried, so a card can sit stale indefinitely -- and until
    parse_vtodo kept PRIORITY/DUE, nothing could even see it had happened.

    One multiget per lane, not one GET per task.
    """
    cards: dict[str, dict] = {}
    for lane, (collection, _) in LANES.items():
        try:
            for item in caldav.list_todos(collection):
                cards[item["uid"]] = dict(item, lane=lane)
        except caldav.CalDavError as e:
            print(f"[audit] {lane}: {e}", flush=True)

    out = []
    for row in store.active_tasks(conn):
        tid = row["id"]
        want_pri = _priority(row["score"] or 0.0)
        want_lane = LANE_ALIASES.get(row["lane"], row["lane"])
        card = cards.get(tid)
        if card is None:
            out.append({"id": tid, "summary": row["title"], "problem": "missing from the board",
                        "want": {"lane": want_lane, "priority": want_pri}, "got": None})
            continue
        bad = {}
        if card.get("priority") != want_pri:
            bad["priority"] = {"want": want_pri, "got": card.get("priority")}
        if card.get("lane") != want_lane:
            bad["lane"] = {"want": want_lane, "got": card.get("lane")}
        want_alarm = intended_alarm(row) is not None
        if bool(card.get("has_alarm")) != want_alarm:
            bad["alarm"] = {"want": want_alarm, "got": bool(card.get("has_alarm"))}
        if bad:
            out.append({"id": tid, "summary": row["title"], "problem": "stale card", "diff": bad})
    return out


def intended_alarm(row, cfg: dict | None = None) -> datetime | None:
    """The alarm this card SHOULD carry.

    alarm_time_for() only knows about deadlines, so it returns None for a task
    with no due date. But promotion and escalation both place alarms
    deliberately and record them in last_alarm_at. Re-pushing a card from
    alarm_time_for() alone therefore strips those -- which silently undoes a
    promotion announcement. Everything that writes or audits a card must agree
    on this one answer.
    """
    at = alarm_time_for(row, cfg)
    if at is not None:
        return at
    raw = row["last_alarm_at"]
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def reconcile(conn) -> dict:
    """Read every brain-owned lane and make the database agree with the phone.

    Two gestures, two meanings — this is the distinction the HA design got
    wrong, where deleting a card deleted the task:

      tick it off   the thing is done      -> mark the task complete
      delete it     dismiss the nudge      -> task untouched, no re-reminder today
    """
    result = {"completed": 0, "reopened": 0, "moved": 0, "dismissed": 0,
              "edited": 0, "filed_to_done": 0}
    seen: set[str] = set()
    newly_done: list[str] = []

    for lane, (collection, _) in LANES.items():
        for item in caldav.list_todos(collection):
            task_id = item["uid"]
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                continue                       # not ours; yours to keep
            seen.add(task_id)

            if row["caldav_collection"] != collection:
                conn.execute("UPDATE tasks SET caldav_collection=? WHERE id=?",
                             (collection, task_id))
            if not same_lane(row["lane"], lane):   # you moved it between lists
                conn.execute(
                    "UPDATE tasks SET lane=?, touched_at=?, updated_at=? WHERE id=?",
                    (lane, store_now(), store_now(), task_id))
                result["moved"] += 1

            done = item["status"] == "COMPLETED" or lane == "done"
            if done and row["status"] != "completed":
                conn.execute(
                    "UPDATE tasks SET status='completed', completed_at=?, updated_at=? WHERE id=?",
                    (store_now(), store_now(), task_id))
                result["completed"] += 1
                if lane != "done":
                    newly_done.append(task_id)
            elif not done and row["status"] == "completed":
                conn.execute(
                    "UPDATE tasks SET status='needs_action', completed_at=NULL WHERE id=?",
                    (task_id,))
                result["reopened"] += 1

            title = (item["summary"] or "").strip()
            if title and title != row["title"]:   # you rewrote it on the phone
                conn.execute("UPDATE tasks SET title=?, updated_at=? WHERE id=?",
                             (title, store_now(), task_id))
                result["edited"] += 1

    # Ticking something off anywhere means it is done, so file it under Done.
    # Deferred until after the loop: moving an item mid-iteration would mutate
    # the collection we are still reading.
    #
    # Swept rather than only tracking this pass's completions, so a task that
    # was already completed in the wrong lane heals itself instead of sitting
    # there forever.
    stuck = [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE archived=0 AND status='completed' AND lane!='done'")]
    for task_id in dict.fromkeys(newly_done + stuck):
        conn.execute("UPDATE tasks SET lane='done' WHERE id=?", (task_id,))
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        try:
            write_task(conn, row)
            result["filed_to_done"] += 1
        except caldav.CalDavError as e:
            print(f"[reconcile] could not file {task_id} under Done: {e}", flush=True)

    # Gone from the list but still live in the database: you swiped it away.
    # That dismisses the reminder for today. It does NOT touch the task.
    for row in conn.execute(
            "SELECT id FROM tasks WHERE archived=0 AND status!='completed' "
            "AND caldav_collection IS NOT NULL"):
        if row["id"] not in seen:
            conn.execute(
                "UPDATE tasks SET caldav_collection=NULL, caldav_etag=NULL, dismissed_until=? "
                "WHERE id=?", (_end_of_day(), row["id"]))
            result["dismissed"] += 1
    return result


def drain_inbox(conn) -> list[str]:
    """Take anything Siri or you added to Inbox and hand it to triage.

    The item is removed from Inbox once captured, so the list is a genuine
    inbox rather than a growing pile.
    """
    import json
    import store

    inbox_dir = store.BRAIN_ROOT / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / f"{datetime.now():%Y-%m-%d}.jsonl"

    captured = []
    for item in caldav.list_todos(INBOX[0]):
        text = (item["summary"] or "").strip()
        if not text:
            continue
        if item["description"]:
            text += "\n" + item["description"]
        rec = {"id": f"c_{store.secrets.token_hex(6)}",
               "at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "local": store.now_iso(), "source": "siri",
               "text": text[:4000], "triaged": False}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        try:
            caldav.delete_todo(INBOX[0], item["uid"])
        except caldav.CalDavError:
            pass
        captured.append(rec["id"])
    return captured


def store_now() -> str:
    import store
    return store.now_iso()


def _end_of_day() -> str:
    now = datetime.now()
    return now.replace(hour=23, minute=59, second=0, microsecond=0).isoformat(timespec="seconds")
