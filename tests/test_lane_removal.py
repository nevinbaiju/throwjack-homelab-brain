"""The upkeep LANE is gone. This is the blast-radius test.

collection_for() raises KeyError, not CalDavError, so every `except
caldav.CalDavError` guarding a board write misses it. One surviving row with
lane='upkeep' would have killed reconcile, the inbox drain and triage outright.
"""
import sys, pathlib, tempfile, os
from datetime import datetime, timedelta
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["BRAIN_ROOT"] = str(TMP)
(TMP / "upkeep.yaml").write_text("version: 2\nchores: []\n")
import store, board, policy, upkeep

fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + detail}")
    if not ok: fails.append(label)

check("the upkeep lane is gone", "upkeep" not in board.LANES)
check("today is a lane", "upkeep" not in board.LANES and "today" in board.LANES)
check("upkeep is not a brain-owned collection", "upkeep" not in board.BRAIN_OWNED)

# The net: this must not raise, whatever is in the database.
try:
    got = board.collection_for("upkeep")
    check("collection_for('upkeep') does not raise", got == "backlog", got)
except Exception as e:
    check("collection_for('upkeep') does not raise", False, f"{type(e).__name__}: {e}")
for lane in ("today", "backlog", "in_progress", "done", "waiting"):
    try:
        board.collection_for(lane)
        check(f"collection_for({lane!r}) works", True)
    except Exception as e:
        check(f"collection_for({lane!r}) works", False, str(e))

check("a capture triaged as upkeep lands in the backlog",
      store.LANE_FOR_TRACK["upkeep"] == "backlog")

# --- in_progress merged into today --------------------------------------
check("the in_progress lane is gone", "in_progress" not in board.LANES)
check("in_progress resolves to today, not an exception",
      board.collection_for("in_progress") == "today")
check("its collection is no longer brain-owned",
      "in-progress" not in board.BRAIN_OWNED)
import project
check("promote is deleted, not just capped", not hasattr(project, "promote"))
check("FOCUS_CAP is gone", not hasattr(project, "FOCUS_CAP"))
check("in_progress_count is gone", not hasattr(store, "in_progress_count"))
check("the `doing` directive now means today",
      project._DIRECTIVE_LANE["doing"][0] == "today")
import plan
check("there is no today cap", not hasattr(plan, "TODAY_CAP"))
check("there is no upkeep cap", not hasattr(plan, "UPKEEP_CAP"))
check("no cap is advertised to the model", "cap of" not in plan.SYSTEM)
check("upkeep is still a track", "upkeep" in store.TRACKS)

# policy still knows the track. Instances carry track='upkeep', and policy.py
# raises KeyError on an unknown one -- so these config keys are load-bearing.
cfg = policy.load_config()
try:
    policy.can_notify("upkeep", datetime(2026, 9, 29, 12, 0), cfg)
    check("policy still has a window for the upkeep track", True)
except KeyError as e:
    check("policy still has a window for the upkeep track", False, str(e))
check("upkeep still has a tolerance_days entry",
      "upkeep" in (cfg.get("tolerance_days") or {}))

conn = store.connect(TMP / "s.db")
now = store.now_iso()

# A row stranded in the dead lane heals itself rather than lurking.
tid = store.create_task(conn, title="Stranded", track="upkeep", lane="upkeep")
check("a stranded row can be created for the test",
      conn.execute("SELECT lane FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "upkeep")
import caldav
caldav.list_todos = lambda c: []
res = board.reconcile(conn)
check("reconcile repoints a row left in the dead lane",
      conn.execute("SELECT lane FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "backlog")
check("...and says it did", res.get("repointed_from_upkeep") == 1, str(res))

# A row stranded in the merged-away lane heals the same way.
tid2 = store.create_task(conn, title="Was in progress", track="focus", lane="in_progress")
res = board.reconcile(conn)
check("reconcile repoints a row left in in_progress to today",
      conn.execute("SELECT lane FROM tasks WHERE id=?", (tid2,)).fetchone()[0] == "today")
check("...and says it did", res.get("repointed_from_in_progress") == 1, str(res))

# An archived row's card is nobody else's job: audit only walks ACTIVE tasks, so
# without this sweep the card lives forever while reconcile keeps reading it.
# Three such cards were sitting in In Progress after the upkeep migration.
orphan = store.create_task(conn, title="Archived but still carded", track="flow")
conn.execute("UPDATE tasks SET archived=1, caldav_collection='backlog' WHERE id=?", (orphan,))
res = board.reconcile(conn)
check("reconcile removes the card of an archived row",
      conn.execute("SELECT caldav_collection FROM tasks WHERE id=?", (orphan,)).fetchone()[0] is None)
check("...and counts it", res.get("orphan_cards_removed") == 1, str(res))
check("the archived row itself is untouched -- nothing is deleted",
      conn.execute("SELECT archived,title FROM tasks WHERE id=?", (orphan,)).fetchone()[0] == 1)
res = board.reconcile(conn)
check("the orphan sweep is idempotent", not res.get("orphan_cards_removed"), str(res))

# board.audit must not resurrect a card you swiped away. This was broken for
# every task, not just chores: audit ignored dismissed_until, so rescore_and_sync
# re-pushed the card within ten minutes WITH its alarm re-armed.
conn2 = store.connect(TMP / "s2.db")
live = store.create_task(conn2, title="Live one", track="flow", lane="today",
                         consequence="costs", due="2026-09-29")
swiped = store.create_task(conn2, title="Swiped away", track="flow", lane="today",
                           consequence="costs", due="2026-09-29")
soon = (datetime.now() + timedelta(hours=5)).isoformat(timespec="seconds")
conn2.execute("UPDATE tasks SET dismissed_until=? WHERE id=?", (soon, swiped))
problems = {p["id"] for p in board.audit(conn2)}
check("audit still reports an ordinary missing card", live in problems, str(problems))
check("audit ignores a card you swiped away today", swiped not in problems, str(problems))
past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
conn2.execute("UPDATE tasks SET dismissed_until=? WHERE id=?", (past, swiped))
check("...but reports it again once the dismissal has expired",
      swiped in {p["id"] for p in board.audit(conn2)})

# _when / alarm_time_for must work from scheduled_at alone.
conn3 = store.connect(TMP / "s3.db")
slotted = store.create_task(conn3, title="Laundry", track="upkeep", lane="today",
                            consequence="costs")
conn3.execute("UPDATE tasks SET scheduled_at='2026-09-29T19:00:00' WHERE id=?", (slotted,))
row = conn3.execute("SELECT * FROM tasks WHERE id=?", (slotted,)).fetchone()
check("_when reads scheduled_at", board._when(row) == datetime(2026, 9, 29, 19, 0))
check("a scheduled_at-only row still gets an alarm",
      board.alarm_time_for(row) is not None)
check("...and intended_alarm agrees, so audit can self-heal",
      board.intended_alarm(row) is not None)
conn3.execute("UPDATE tasks SET scheduled_at=NULL, due='2026-09-29' WHERE id=?", (slotted,))
row = conn3.execute("SELECT * FROM tasks WHERE id=?", (slotted,)).fetchone()
check("a due-only row still falls back to 09:00",
      board._when(row) == datetime(2026, 9, 29, 9, 0))
conn3.execute("UPDATE tasks SET due=NULL WHERE id=?", (slotted,))
row = conn3.execute("SELECT * FROM tasks WHERE id=?", (slotted,)).fetchone()
check("a row with neither has no alarm", board._when(row) is None
      and board.alarm_time_for(row) is None)

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'test_lane_removal: all passed'}")
sys.exit(1 if fails else 0)
