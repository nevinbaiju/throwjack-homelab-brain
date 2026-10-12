"""The Homelab Backlog review queue: an off-board lane with promote / ignore.

The sharp edge is not the buttons. board.audit() walks EVERY active row and
rescore_and_sync() pushes whatever does not match a card, so an unprojected lane
that is merely absent from LANES makes the first item filed there raise KeyError
out of collection_for() -- which no `except caldav.CalDavError` catches, so it
takes reconcile, the inbox drain and triage down with it.
"""
import sys, pathlib, tempfile, os
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["BRAIN_ROOT"] = str(TMP)
(TMP / "upkeep.yaml").write_text("version: 2\nchores: []\n")
import store, board, caldav

fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + str(detail)}")
    if not ok: fails.append(label)

pushed, removed = [], []
caldav.list_todos = lambda coll: []
caldav.ensure_collection = lambda *a, **k: None
caldav.put_todo = lambda coll, uid, ics, etag=None: pushed.append((coll, uid)) or "e1"
caldav.delete_todo = lambda coll, uid: removed.append((coll, uid))

conn = store.connect(TMP / "s.db")

# --- the lane is off-board ----------------------------------------------
check("homelab is declared off-board", "homelab" in board.OFF_BOARD)
check("collection_for returns None rather than raising",
      board.collection_for("homelab") is None)
check("it is not a CalDAV collection", "homelab" not in board.BRAIN_OWNED)
check("...and not in LANES", "homelab" not in board.LANES)
for lane in ("today", "backlog", "done", "waiting", "in_progress", "upkeep"):
    try:
        board.collection_for(lane)
        check(f"collection_for({lane!r}) still resolves", True)
    except Exception as e:
        check(f"collection_for({lane!r}) still resolves", False, f"{type(e).__name__}: {e}")

# --- THE TRAP: audit must not try to push one ---------------------------
item = store.create_task(conn, title="Finish the router power supply swap",
                         track="focus", lane="homelab", consequence="improves")
problems = board.audit(conn)
check("audit does NOT report an off-board item as missing from the board",
      item not in {p["id"] for p in problems}, str(problems))
check("...and audit does not raise", isinstance(problems, list))
row = conn.execute("SELECT * FROM tasks WHERE id=?", (item,)).fetchone()
try:
    board.write_task(conn, row)
    check("write_task on an off-board row is a no-op, not a crash", True)
except Exception as e:
    check("write_task on an off-board row is a no-op, not a crash", False,
          f"{type(e).__name__}: {e}")
check("...and it pushed nothing", pushed == [], str(pushed))
check("no card was recorded for it",
      conn.execute("SELECT caldav_collection FROM tasks WHERE id=?",
                   (item,)).fetchone()[0] is None)
check("it carries no alarm", board.intended_alarm(row) is None)

# An ordinary backlog row still audits as missing, so the guard is not too broad.
normal = store.create_task(conn, title="Ordinary task", track="flow",
                           lane="backlog", consequence="costs")
check("an ordinary row IS still reported missing",
      normal in {p["id"] for p in board.audit(conn)})

# Moving a task INTO the queue takes its card off the phone, rather than
# orphaning it where nothing audits it.
conn.execute("UPDATE tasks SET caldav_collection='backlog' WHERE id=?", (normal,))
conn.execute("UPDATE tasks SET lane='homelab' WHERE id=?", (normal,))
removed.clear()
board.write_task(conn, conn.execute("SELECT * FROM tasks WHERE id=?", (normal,)).fetchone())
check("moving a task into the queue removes its card",
      removed == [("backlog", normal)], str(removed))

# --- a full triage pass must survive the lane --------------------------
import triage, llm
triage.project_to_board = lambda c, t: None
triage.sync_rollups = lambda c, cfg=None: {}
triage.reorder_lanes = lambda c: {}
triage._escalate = lambda c: {}
triage._sweep_upkeep = lambda c: {}
triage.ingest_inbox = lambda c: 0
try:
    out = triage.run(conn)
    check("a triage pass completes with items in the queue", True)
    check("...and the queue item was not carded", not any(
        p for p in board.audit(conn) if p["id"] == item), str(out.get("carded")))
except Exception as e:
    check("a triage pass completes with items in the queue", False,
          f"{type(e).__name__}: {e}")

# --- the planner must never offer an unreviewed item -------------------
import plan, policy
ctx = plan.build_context(conn, today=policy.local_today())
ids = {t["id"] for group in ctx["lanes"].values() for t in group}
check("build_context does not show the review queue to the planner",
      item not in ids, str(ctx["lanes"].keys()))
check("the planner only sees today and backlog",
      set(ctx["lanes"]) == {"today", "backlog"}, str(set(ctx["lanes"])))

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'test_homelab: all passed'}")
sys.exit(1 if fails else 0)
