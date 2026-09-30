"""Chore state and instances: last_done, one-instance-per-day, re-slotting."""
import sys, pathlib, tempfile, os
from datetime import date, timedelta
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["BRAIN_ROOT"] = str(TMP)
import store, board as bd
removed = []
bd.remove_task = lambda c, r: removed.append(r["id"])
import upkeep

fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + detail}")
    if not ok: fails.append(label)

def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
        return False
    except exc:
        return True


def yaml_with(*chores):
    p = TMP / f"chores{len(list(TMP.glob('chores*')))}.yaml"
    body = "version: 2\nchores:\n"
    for c in chores:
        body += f"  - id: {c['id']}\n    title: {c['title']}\n    every: {c['every']}\n"
        if c.get("window"): body += f"    window: {c['window']}\n"
        if c.get("est_min"): body += f"    est_min: {c['est_min']}\n"
        if c.get("essential"): body += "    essential: true\n"
    p.write_text(body)
    upkeep._cache = None
    return p

conn = store.connect(TMP / "s.db")
LAUNDRY = upkeep._check({"id": "laundry", "title": "Laundry", "every": "7d",
                         "est_min": 20}, 0)
SHEETS = upkeep._check({"id": "sheets", "title": "Change the sheets",
                        "every": "14d", "window": "weekend"}, 0)
TODAY = date(2026, 9, 29)

# --- upkeep_state ---------------------------------------------------------
check("table exists", bool(conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='upkeep_state'").fetchone()))
check("source_key is indexed", bool(conn.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_tasks_source'").fetchone()))

upkeep.record_done(conn, "laundry", date(2026, 9, 20))
check("record_done writes last_done",
      upkeep.state_row(conn, "laundry")["last_done"] == "2026-09-20")
upkeep.record_done(conn, "laundry", date(2026, 9, 20))
check("record_done is idempotent",
      upkeep.state_row(conn, "laundry")["last_done"] == "2026-09-20")
upkeep.record_done(conn, "laundry", date(2026, 9, 10))
check("record_done never moves last_done backwards",
      upkeep.state_row(conn, "laundry")["last_done"] == "2026-09-20")
upkeep.record_done(conn, "laundry", date(2026, 9, 27))
check("record_done moves forwards",
      upkeep.state_row(conn, "laundry")["last_done"] == "2026-09-27")

seen = upkeep.ensure_first_seen(conn, "brand-new", TODAY)
check("first_seen is recorded on first sight", seen == TODAY)
check("first_seen is stable",
      upkeep.ensure_first_seen(conn, "brand-new", TODAY + timedelta(days=5)) == TODAY)

# --- instances ------------------------------------------------------------
tid = upkeep.materialise(conn, LAUNDRY, "19:00", day=TODAY, why="clothes pile")
row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
check("instance is a today-lane upkeep task",
      (row["lane"], row["track"], row["status"]) == ("today", "upkeep", "needs_action"),
      str(dict(row)))
check("instance carries the source key",
      row["source_key"] == f"upkeep:laundry:{TODAY}", row["source_key"])
check("instance carries scheduled_at", row["scheduled_at"] == f"{TODAY}T19:00:00")
check("instance carries due (so urgency has a deadline branch)",
      row["due"] == TODAY.isoformat())
check("instance takes est_min from the chore", row["estimate_min"] == 20)
check("non-essential chore gets a consequence, not NULL",
      row["consequence"] == "costs", str(row["consequence"]))
check("why is carried", row["why"] == "clothes pile")

ess = upkeep._check({"id": "bins", "title": "Bins", "every": "7d", "essential": True}, 0)
eid = upkeep.materialise(conn, ess, "08:00", day=TODAY)
check("essential chore gets consequence=breaks",
      conn.execute("SELECT consequence FROM tasks WHERE id=?", (eid,)).fetchone()[0] == "breaks")

# Two schedules for one chore on one day is ONE row, re-slotted.
again = upkeep.materialise(conn, LAUNDRY, "21:30", day=TODAY, why="after dinner")
check("scheduling twice re-slots rather than duplicating", again == tid)
row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
check("re-slot takes the new time", row["scheduled_at"] == f"{TODAY}T21:30:00")
check("re-slot takes the new why", row["why"] == "after dinner")
check("only one instance exists for the day", conn.execute(
    "SELECT COUNT(*) c FROM tasks WHERE source_key=?",
    (f"upkeep:laundry:{TODAY}",)).fetchone()["c"] == 1)
check("re-slot clears last_alarm_at so the audit re-pushes",
      row["last_alarm_at"] is None)

check("instance_for finds it", upkeep.instance_for(conn, "laundry", TODAY)["id"] == tid)
check("instance_for is day-scoped",
      upkeep.instance_for(conn, "laundry", TODAY + timedelta(days=1)) is None)
before_n = conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
check("a bad slot is refused", raises(upkeep.ChoreError, upkeep.materialise,
                                     conn, LAUNDRY, "7pm", day=TODAY))
check("a refused slot writes nothing",
      conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == before_n)

# --- archive / un-archive -------------------------------------------------
removed.clear()
upkeep.archive_instance(conn, row, "test")
check("archive sets archived=1",
      conn.execute("SELECT archived FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 1)
check("archive removes the card", removed == [tid], str(removed))
check("instance_for hides an archived row",
      upkeep.instance_for(conn, "laundry", TODAY) is None)
check("instance_for(include_archived) still finds it",
      upkeep.instance_for(conn, "laundry", TODAY, include_archived=True)["id"] == tid)

# Escalation must NOT resurrect something that was archived.
check("automatic materialise refuses when an archived instance exists",
      upkeep.materialise(conn, LAUNDRY, "19:00", day=TODAY, automatic=True) is None)
# ...but the user asking for it wins.
back = upkeep.materialise(conn, LAUNDRY, "20:00", day=TODAY)
check("a user schedule un-archives the same row", back == tid)
row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
check("un-archived instance is live again in today",
      (row["archived"], row["lane"], row["status"]) == (0, "today", "needs_action"))

# --- completion hooks ----------------------------------------------------
conn.execute("UPDATE tasks SET status='completed', completed_at=? WHERE id=?",
             (f"{TODAY}T20:31:00", tid))
row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
check("note_completion recognises an instance", upkeep.note_completion(conn, row) == "laundry")
check("completing an instance sets last_done",
      upkeep.state_row(conn, "laundry")["last_done"] == TODAY.isoformat())
plain = store.create_task(conn, title="Email Dr Rao", track="flow")
check("note_completion ignores an ordinary task", upkeep.note_completion(
    conn, conn.execute("SELECT * FROM tasks WHERE id=?", (plain,)).fetchone()) is None)

# Un-ticking on the phone recomputes from what survives, and falls back to seed.
upkeep._touch_state(conn, "laundry", seed_done="2026-09-01")
conn.execute("UPDATE tasks SET status='needs_action', completed_at=NULL WHERE id=?", (tid,))
upkeep.note_reopened(conn, conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
check("reopening falls back to seed_done when no completion survives",
      upkeep.state_row(conn, "laundry")["last_done"] == "2026-09-01",
      str(upkeep.state_row(conn, "laundry")["last_done"]))

# --- rename keeps state --------------------------------------------------
p = yaml_with({"id": "sheets", "title": "Change the sheets", "every": "14d",
               "window": "weekend"})
upkeep.record_done(conn, "sheets", date(2026, 9, 5))
before = upkeep.all_states(conn, date(2026, 9, 26), p)[0]
p2 = yaml_with({"id": "sheets", "title": "Strip and remake the bed", "every": "14d",
                "window": "weekend"})
after = upkeep.all_states(conn, date(2026, 9, 26), p2)[0]
check("renaming the title keeps last_done (stable id)",
      before["last_done"] == after["last_done"] == "2026-09-05")
check("renaming keeps the computed state",
      (before["state"], before["overdue_days"]) == (after["state"], after["overdue_days"]))
check("the new title is what shows", after["title"] == "Strip and remake the bed")

# --- all_states ----------------------------------------------------------
p = yaml_with({"id": "sheets", "title": "Change the sheets", "every": "14d",
               "window": "weekend"},
              {"id": "laundry", "title": "Laundry", "every": "7d"})
states = {c["slug"]: c for c in upkeep.all_states(conn, date(2026, 9, 26), p)}
check("all_states covers every chore in the yaml", set(states) == {"sheets", "laundry"})
check("all_states computes sheets as due on the Saturday",
      (states["sheets"]["state"], states["sheets"]["overdue_days"],
       states["sheets"]["missed_windows"]) == ("due", 7, 1), str(states["sheets"]))

# A chore deleted from the yaml must not come back from its state row.
p = yaml_with({"id": "laundry", "title": "Laundry", "every": "7d"})
check("a chore deleted from the yaml is gone, despite its state row surviving",
      [c["slug"] for c in upkeep.all_states(conn, date(2026, 9, 26), p)] == ["laundry"])

# A broken chore is reported, not raised, and does not hide its neighbours.
bad = TMP / "bad.yaml"
bad.write_text("version: 2\nchores:\n  - id: ok\n    title: Fine\n    every: 7d\n"
               "  - title: Broken\n    every: sometimes\n")
upkeep._cache = None
chores, errors = upkeep.load_with_errors(bad)
check("a broken chore is collected as an error, not raised",
      [c["slug"] for c in chores] == ["ok"] and len(errors) == 1, str(errors))
check("the error names the chore", "Broken" in errors[0], errors[0])

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'test_upkeep_state: all passed'}")
sys.exit(1 if fails else 0)
