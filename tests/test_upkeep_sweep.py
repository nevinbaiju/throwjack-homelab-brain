"""The overnight sweep. What happens to a chore you did not do.

The requirement: not picking a chore keeps it coming back. So an instance that
outlives its day is archived -- not restored to a previous lane, not left on the
phone, and without touching last_done, so the chore returns Due with its overdue
count grown.
"""
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

LAUNDRY = upkeep._check({"id": "laundry", "title": "Laundry", "every": "7d"}, 0)
CHORES = TMP / "chores.yaml"
CHORES.write_text("version: 2\nchores:\n  - id: laundry\n    title: Laundry\n"
                  "    every: 7d\n")

TODAY = date(2026, 9, 29)
YDAY = TODAY - timedelta(days=1)

def fresh():
    upkeep._cache = None
    conn = store.connect(TMP / f"s{len(list(TMP.glob('s*.db')))}.db")
    return conn

def row_of(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()

# --- yesterday, not done -> archived -------------------------------------
conn = fresh(); removed.clear()
upkeep.record_done(conn, "laundry", date(2026, 9, 15))
tid = upkeep.materialise(conn, LAUNDRY, "19:00", day=YDAY)
conn.execute("UPDATE tasks SET caldav_collection='today' WHERE id=?", (tid,))
res = upkeep.sweep_expired(conn, TODAY, reconciled=True)
r = row_of(conn, tid)
check("yesterday's unfinished instance is archived", r["archived"] == 1)
check("its card is removed from the phone", removed == [tid], str(removed))
check("the sweep counts it", res["archived"] == 1, str(res))
check("it is NOT restored to a previous lane -- it stays out of the board",
      r["lane"] == "today" and r["archived"] == 1, f"lane={r['lane']}")
check("last_done is untouched",
      upkeep.state_row(conn, "laundry")["last_done"] == "2026-09-15")
state = upkeep.all_states(conn, TODAY, CHORES)[0]
check("the chore is Due again, overdue grown",
      (state["state"], state["overdue_days"]) == ("due", 7), str(state))
check("and it no longer reports a scheduled instance for today",
      state["scheduled_today"] is None)

# Running it twice changes nothing more.
res2 = upkeep.sweep_expired(conn, TODAY, reconciled=True)
check("the sweep is idempotent", res2["archived"] == 0, str(res2))

# --- yesterday, completed -> left alone ----------------------------------
conn = fresh(); removed.clear()
tid = upkeep.materialise(conn, LAUNDRY, "19:00", day=YDAY)
conn.execute("UPDATE tasks SET status='completed', completed_at=?, lane='done' WHERE id=?",
             (f"{YDAY}T19:40:00", tid))
upkeep.record_done(conn, "laundry", YDAY)
res = upkeep.sweep_expired(conn, TODAY, reconciled=True)
r = row_of(conn, tid)
check("a completed instance is not archived by the sweep", r["archived"] == 0)
check("it stays filed under Done", r["lane"] == "done")
check("its card is left alone", removed == [], str(removed))
check("last_done survives", upkeep.state_row(conn, "laundry")["last_done"] == str(YDAY))
state = upkeep.all_states(conn, TODAY, CHORES)[0]
check("a chore done yesterday is not due today", state["state"] == "not_due", str(state))

# --- today -> left alone -------------------------------------------------
conn = fresh(); removed.clear()
tid = upkeep.materialise(conn, LAUNDRY, "19:00", day=TODAY)
upkeep.sweep_expired(conn, TODAY, reconciled=True)
check("today's instance is left alone", row_of(conn, tid)["archived"] == 0)
check("today's card is left alone", removed == [])

# --- dragged to another lane on the phone -> still archived --------------
# The sweep keys on the date in source_key, not on the lane. A lane-keyed sweep
# would never see this row again and the chore would be stuck forever.
conn = fresh(); removed.clear()
tid = upkeep.materialise(conn, LAUNDRY, "19:00", day=YDAY)
conn.execute("UPDATE tasks SET lane='backlog', caldav_collection='backlog' WHERE id=?", (tid,))
upkeep.sweep_expired(conn, TODAY, reconciled=True)
check("an instance dragged to Backlog is still archived", row_of(conn, tid)["archived"] == 1)
check("and its card is still removed", removed == [tid])

conn = fresh(); removed.clear()
tid = upkeep.materialise(conn, LAUNDRY, "19:00", day=YDAY)
conn.execute("UPDATE tasks SET lane='in_progress' WHERE id=?", (tid,))
upkeep.sweep_expired(conn, TODAY, reconciled=True)
check("same for In Progress", row_of(conn, tid)["archived"] == 1)

# --- ordering invariant --------------------------------------------------
conn = fresh(); removed.clear()
tid = upkeep.materialise(conn, LAUNDRY, "19:00", day=YDAY)
try:
    upkeep.sweep_expired(conn, TODAY, reconciled=False)
    check("the sweep refuses to run before reconcile", False)
except upkeep.SweepOrderError:
    check("the sweep refuses to run before reconcile", True)
check("a refused sweep changes nothing", row_of(conn, tid)["archived"] == 0)
check("a refused sweep touches no cards", removed == [])

# --- pruning -------------------------------------------------------------
conn = fresh(); removed.clear()
old_day = TODAY - timedelta(days=upkeep.PRUNE_COMPLETED_DAYS + 1)
keep_day = TODAY - timedelta(days=2)
old_id = upkeep.materialise(conn, LAUNDRY, "19:00", day=old_day)
keep_id = upkeep.materialise(conn, LAUNDRY, "19:00", day=keep_day)
for tid_, d in ((old_id, old_day), (keep_id, keep_day)):
    conn.execute("UPDATE tasks SET status='completed', completed_at=?, lane='done' "
                 "WHERE id=?", (f"{d}T19:40:00", tid_))
res = upkeep.sweep_expired(conn, TODAY, reconciled=True)
check("a long-finished instance is pruned", row_of(conn, old_id)["archived"] == 1)
check("a recently-finished instance is kept", row_of(conn, keep_id)["archived"] == 0)
check("pruning is counted separately from archiving",
      (res["pruned"], res["archived"]) == (1, 0), str(res))

# --- non-chore rows are never touched ------------------------------------
conn = fresh(); removed.clear()
plain = store.create_task(conn, title="Email Dr Rao", track="flow", lane="today",
                          due=str(YDAY))
nudge = store.create_task(conn, title="Old chore nudge", track="upkeep", lane="backlog")
conn.execute("UPDATE tasks SET source_key='chore:u_laundry' WHERE id=?", (nudge,))
upkeep.sweep_expired(conn, TODAY, reconciled=True)
check("an ordinary overdue task is not swept", row_of(conn, plain)["archived"] == 0)
check("a legacy chore: nudge is not swept", row_of(conn, nudge)["archived"] == 0)
check("no cards were touched", removed == [])

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'test_upkeep_sweep: all passed'}")
sys.exit(1 if fails else 0)
