"""Escalation. Mostly a proof that it stays quiet."""
import sys, pathlib, tempfile, os
from datetime import date, datetime, timedelta
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["BRAIN_ROOT"] = str(TMP)
CHORES = TMP / "upkeep.yaml"
CHORES.write_text("version: 2\nchores:\n  - id: laundry\n    title: Laundry\n    every: 7d\n")
import store, board as bd
written, removed = [], []
bd.write_task = lambda c, r, **k: written.append((r["id"], k.get("alarm_at")))
bd.remove_task = lambda c, r: removed.append(r["id"])
import escalate, upkeep, policy

fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + detail}")
    if not ok: fails.append(label)

def fresh():
    upkeep._cache = None
    return store.connect(TMP / f"s{len(list(TMP.glob('s*.db')))}.db")

NOW = datetime(2026, 9, 29, 12, 0, 0)
TODAY = NOW.date()

def set_chores(text):
    CHORES.write_text(text)
    upkeep._cache = None

# --- the quiet case: nothing is essential --------------------------------
conn = fresh()
upkeep.record_done(conn, "laundry", date(2026, 8, 1))      # wildly overdue
res = escalate.escalate_chores(conn, NOW)
check("a non-essential chore is never escalated, however overdue",
      res["scheduled"] == 0 and res["watched"] == 0, str(res))
check("nothing was written to the board", written == [], str(written))
state = upkeep.all_states(conn, TODAY)[0]
check("it is still just... due", state["state"] == "due", str(state))

# --- a broken chore file must not raise out of run() ---------------------
conn = fresh()
set_chores("version: 2\nchores:\n  - title: Broken\n    every: sometimes\n")
try:
    out = escalate.run(conn, NOW)
    check("escalate.run survives a malformed chore file", True)
    check("...and reports rather than crashing", "chores" in out, str(out))
except Exception as e:
    check("escalate.run survives a malformed chore file", False, f"{type(e).__name__}: {e}")
set_chores("version: 2\nchores: !!!not yaml at all: [\n")
try:
    escalate.run(conn, NOW)
    check("escalate.run survives an unparseable chore file", True)
except Exception as e:
    check("escalate.run survives an unparseable chore file", False, f"{type(e).__name__}: {e}")

# --- an essential chore past grace ---------------------------------------
conn = fresh(); written.clear()
set_chores("version: 2\nchores:\n  - id: bins\n    title: Bins\n    every: 7d\n"
           "    essential: true\n    escalate_after: 2d\n    time_of_day: evening\n")
upkeep.record_done(conn, "bins", TODAY - timedelta(days=10))    # due 3d ago
res = escalate.escalate_chores(conn, NOW)
check("an essential chore past grace is scheduled", res["scheduled"] == 1, str(res))
inst = upkeep.instance_for(conn, "bins", TODAY)
check("the instance lands on today", inst is not None and inst["lane"] == "today")
check("at the time of day the chore asked for",
      inst["scheduled_at"].endswith("T19:00:00"), str(inst["scheduled_at"]))
check("and it gets a card with an alarm", len(written) == 1 and written[0][1] is not None,
      str(written))
check("the alarm is in the future, never the past",
      written[0][1] > NOW, str(written[0][1]))

# Twice in one day is once.
written.clear()
res = escalate.escalate_chores(conn, NOW)
check("escalation does not schedule a second time the same day",
      res["scheduled"] == 0 and res["already"] == 1, str(res))
check("and writes no second card", written == [])

# Dismissed or swept -> must NOT come back.
upkeep.archive_instance(conn, inst, "dismissed")
written.clear()
res = escalate.escalate_chores(conn, NOW)
check("escalation never resurrects an instance you got rid of",
      res["scheduled"] == 0, str(res))
check("...and writes nothing", written == [])

# Within grace -> silent.
conn = fresh(); written.clear()
upkeep.record_done(conn, "bins", TODAY - timedelta(days=8))     # 1d overdue, grace 2d
res = escalate.escalate_chores(conn, NOW)
check("within grace, nothing happens", res["scheduled"] == 0, str(res))

# A windowed essential chore is not scheduled outside its window.
conn = fresh(); written.clear()
set_chores("version: 2\nchores:\n  - id: sheets\n    title: Change the sheets\n"
           "    every: 7d\n    window: weekend\n    essential: true\n    escalate_after: 1d\n")
upkeep.record_done(conn, "sheets", date(2026, 9, 5))
TUE = datetime(2026, 9, 29, 12, 0)                              # a Tuesday
check("Tuesday really is a weekday", TUE.weekday() == 1)
res = escalate.escalate_chores(conn, TUE)
check("a weekend chore is not auto-scheduled on a Tuesday",
      res["scheduled"] == 0, str(res))
check("...because it is waiting, not due",
      upkeep.all_states(conn, TUE.date())[0]["state"] == "waiting")
SAT = datetime(2026, 10, 3, 12, 0)
res = escalate.escalate_chores(conn, SAT)
check("but it IS scheduled on the Saturday", res["scheduled"] == 1, str(res))

# --- escalate_tasks leaves instances alone -------------------------------
conn = fresh(); written.clear()
set_chores("version: 2\nchores:\n  - id: laundry\n    title: Laundry\n    every: 7d\n")
tid = upkeep.materialise(conn, upkeep.by_slug("laundry"), "19:00", day=TODAY)
conn.execute("UPDATE tasks SET caldav_collection='today', last_alarm_at=? WHERE id=?",
             ((NOW - timedelta(hours=3)).isoformat(timespec="seconds"), tid))
res = escalate.escalate_tasks(conn, NOW)
check("a chore instance with a passed alarm is NOT laddered",
      res["rearmed"] == 0, str(res))
check("...and gets no new card", written == [], str(written))
check("...and its escalation count stays at zero",
      (conn.execute("SELECT escalations FROM tasks WHERE id=?", (tid,)).fetchone()[0] or 0) == 0)

# An ordinary task with a passed alarm still escalates -- the ladder works.
plain = store.create_task(conn, title="Email Dr Rao", track="flow", lane="today",
                          consequence="costs", due=TODAY.isoformat())
conn.execute("UPDATE tasks SET caldav_collection='today', last_alarm_at=? WHERE id=?",
             ((NOW - timedelta(hours=3)).isoformat(timespec="seconds"), plain))
res = escalate.escalate_tasks(conn, NOW)
check("an ordinary task still escalates", res["rearmed"] == 1, str(res))

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'test_upkeep_escalate: all passed'}")
sys.exit(1 if fails else 0)
