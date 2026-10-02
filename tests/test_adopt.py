"""Cards you create on your phone become real tasks.

reconcile is identity-keyed -- it looks a card up by uid, and the brain mints its
own ids -- so anything typed straight into a list had no row and was skipped
forever with "not ours; yours to keep". Only Inbox was ever ingested.
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

# A phone-made card: an iOS UUID, not a t_ id.
UUID = "597FE7E5-7B21-4337-B24D-50772E0829DF"
def card(uid, summary, status="NEEDS-ACTION", desc=""):
    return {"uid": uid, "summary": summary, "status": status, "description": desc,
            "completed_at": None, "priority": None, "due": None,
            "has_alarm": False, "alarm_trigger": None, "etag": "etag1"}

CARDS = {}
caldav.list_todos = lambda coll: CARDS.get(coll, [])
board.write_task = lambda c, r, **k: None
board.remove_task = lambda c, r: c.execute(
    "UPDATE tasks SET caldav_collection=NULL WHERE id=?", (r["id"],))

conn = store.connect(TMP / "s.db")

# --- the basic case ------------------------------------------------------
CARDS = {"backlog": [card(UUID, "Routers power supply")]}
res = board.reconcile(conn)
row = conn.execute("SELECT * FROM tasks WHERE id=?", (UUID,)).fetchone()
check("a card you made on your phone becomes a task", row is not None)
check("reconcile counts it", res.get("adopted") == 1, str(res))
check("the iPhone's own uuid IS the task id", row["id"] == UUID)
check("your title is kept exactly", row["title"] == "Routers power supply")
check("it lands in the lane you put it in", row["lane"] == "backlog")
check("it is linked to its card, so pushes go to the right collection",
      row["caldav_collection"] == "backlog")
check("provenance is recorded", row["source_key"] == "phone:backlog")
check("it has a consequence, so it does not score 0 / PRIORITY 9",
      row["consequence"] == "costs", str(row["consequence"]))

# Idempotent: the second pass finds the row by id and never reaches adopt().
res = board.reconcile(conn)
check("adopting is idempotent", not res.get("adopted"), str(res))
check("and does not duplicate the task", conn.execute(
    "SELECT COUNT(*) c FROM tasks WHERE title='Routers power supply'").fetchone()["c"] == 1)

# --- a capture is written, pointing AT the task --------------------------
cap = conn.execute("SELECT * FROM captures WHERE task_id=?", (UUID,)).fetchone()
check("a capture is written so triage can classify it", cap is not None)
check("the capture points at the adopted task, which is what tells triage "
      "to enrich rather than create", cap and cap["task_id"] == UUID)
check("the capture is still pending", cap and cap["state"] == "new")
check("it is marked as coming from the phone", cap and cap["source"] == "phone-adopt")

# --- Today is adoptable, Done is not ------------------------------------
conn2 = store.connect(TMP / "s2.db")
CARDS = {"today":   [card("UUID-T", "Server fix")],
         "backlog": [card("UUID-B", "Return soldering")],
         "done":    [card("UUID-D", "Something I finished")]}
board.reconcile(conn2)
check("a card in Today is adopted into today",
      (conn2.execute("SELECT lane FROM tasks WHERE id='UUID-T'").fetchone() or [None])[0] == "today")
check("a card in Backlog is adopted into backlog",
      (conn2.execute("SELECT lane FROM tasks WHERE id='UUID-B'").fetchone() or [None])[0] == "backlog")
check("a card in Done is LEFT ALONE",
      conn2.execute("SELECT * FROM tasks WHERE id='UUID-D'").fetchone() is None)

# --- what must NOT be adopted -------------------------------------------
conn3 = store.connect(TMP / "s3.db")
# An archived task's surviving card must never come back as a new task.
gone = store.create_task(conn3, title="Archived nudge", track="upkeep", lane="backlog")
conn3.execute("UPDATE tasks SET archived=1, caldav_collection='backlog' WHERE id=?", (gone,))
CARDS = {"backlog": [card(gone, "Archived nudge")]}
res = board.reconcile(conn3)
check("an archived task's card is NOT re-adopted", not res.get("adopted"), str(res))
check("...it stays archived",
      conn3.execute("SELECT archived FROM tasks WHERE id=?", (gone,)).fetchone()[0] == 1)
check("...and its card is swept instead",
      conn3.execute("SELECT caldav_collection FROM tasks WHERE id=?",
                    (gone,)).fetchone()[0] is None)
check("exactly one row exists for it", conn3.execute(
    "SELECT COUNT(*) c FROM tasks WHERE title='Archived nudge'").fetchone()["c"] == 1)

# An empty card is not a task.
conn4 = store.connect(TMP / "s4.db")
CARDS = {"backlog": [card("UUID-EMPTY", "   ")]}
res = board.reconcile(conn4)
check("a blank card is not adopted", not res.get("adopted"), str(res))

# Shopping lists are not lanes, so they are never touched. (They are not in
# LANES at all -- this pins that, because adopting them would turn every grocery
# item into a task.)
check("shopping collections are not brain lanes",
      not any(c.startswith("shop-") for c, _ in board.LANES.values()))
check("inbox is not a lane either -- it has its own draining path",
      "inbox" not in {c for c, _ in board.LANES.values()})

# --- a completed card you add by hand -----------------------------------
conn5 = store.connect(TMP / "s5.db")
CARDS = {"backlog": [card("UUID-DONE", "Temu BT closure", status="COMPLETED")]}
board.reconcile(conn5)
r = conn5.execute("SELECT * FROM tasks WHERE id='UUID-DONE'").fetchone()
check("a card already ticked is adopted as completed",
      r is not None and r["status"] == "completed", str(dict(r) if r else None))
check("...and reconcile files it under Done", r and r["lane"] == "done", r and r["lane"])

# --- the description becomes the note -----------------------------------
conn6 = store.connect(TMP / "s6.db")
CARDS = {"backlog": [card("UUID-N", "Stem opt closure", desc="ask Priya first")]}
board.reconcile(conn6)
check("the card's notes become the task's note",
      conn6.execute("SELECT note FROM tasks WHERE id='UUID-N'").fetchone()[0] == "ask Priya first")

# --- find_duplicate must not steal the enrichment -----------------------
# An adopted task has the same normalised title on the same track as its own
# capture, so if triage checked for duplicates FIRST it would match the adopted
# row, mark the capture "done" and throw the classification away.
conn7 = store.connect(TMP / "s7.db")
CARDS = {"backlog": [card("UUID-D2", "Server fix")]}
board.reconcile(conn7)
dupe = store.find_duplicate(conn7, "Server fix", "flow")
check("find_duplicate DOES match an adopted task (hence the ordering in triage)",
      dupe == "UUID-D2", str(dupe))

# --- triage ENRICHES the adopted row, it does not create a second one -----
# This is the half the endpoint tests cannot see. Two traps live here:
#   1. find_duplicate matches the adopted task by title, so if it ran first the
#      classification would be discarded as a "duplicate".
#   2. the prompt's title rule would rewrite "Server fix" into a verb phrase,
#      which is exactly the text you are looking at on your phone.
import triage, llm, json as _json

conn8 = store.connect(TMP / "s8.db")
caldav.ensure_collection = lambda *a, **k: None
caldav.put_todo = lambda *a, **k: None
caldav.delete_todo = lambda *a, **k: None
triage.project_to_board = lambda c, t: None
triage.sync_rollups = lambda c, cfg=None: {}
triage.reorder_lanes = lambda c: {}
triage.rescore_and_sync = lambda c, summary=None: {}
triage._escalate = lambda c: {}
triage._sweep_upkeep = lambda c: {}
triage.ingest_inbox = lambda c: 0

CARDS = {"backlog": [card("UUID-ENRICH", "Server fix", desc="ssh keeps dropping")]}
llm.complete = lambda *a, **k: ("{}", "stub-model", 1)
llm.validate_triage = lambda raw, captures: [{
    "capture_id": captures[0]["id"],
    # What a real model returns: a rewritten title, and a classification.
    "title": "Diagnose why the server drops SSH connections",
    "track": "focus", "consequence": "breaks", "project": None,
    "estimate_min": 45, "due": None, "blocked_on": None, "note": "",
}]
out = triage.run(conn8)

rows = conn8.execute("SELECT * FROM tasks").fetchall()
check("exactly one task exists -- no second row was created", len(rows) == 1, str(len(rows)))
r = rows[0]
check("it is still the adopted row, keyed by the iPhone uuid", r["id"] == "UUID-ENRICH")
check("YOUR title survived the model's rewrite", r["title"] == "Server fix", r["title"])
check("the model's track was applied", r["track"] == "focus", r["track"])
check("the model's consequence was applied", r["consequence"] == "breaks", r["consequence"])
check("the lane you chose was not touched", r["lane"] == "backlog", r["lane"])
check("triage counts it as adopted, not triaged",
      out.get("adopted") == 1 and not out.get("triaged"), str(out))
check("it is NOT counted as a duplicate", not out.get("duplicates"), str(out))
check("the capture is closed against the adopted task",
      conn8.execute("SELECT state,task_id FROM captures").fetchone()["task_id"] == "UUID-ENRICH")

# A "P1:" you typed on the card is a directive, so it sets the priority -- but
# the prefix is not stripped, because that would be rewriting your text.
conn9 = store.connect(TMP / "s9.db")
CARDS = {"backlog": [card("UUID-P1", "P1: Replace the CMOS battery")]}
llm.validate_triage = lambda raw, captures: [{
    "capture_id": captures[0]["id"], "title": "Replace the CMOS battery",
    "track": "flow", "consequence": "improves", "project": None,
    "estimate_min": 10, "due": None, "blocked_on": None, "note": "",
}]
triage.run(conn9)
r = conn9.execute("SELECT * FROM tasks WHERE id='UUID-P1'").fetchone()
check("a P-level typed on the card sets the consequence (P1 -> breaks), "
      "overriding what the model guessed",
      r["consequence"] == "breaks", r["consequence"])
check("...and the title is still exactly what you typed",
      r["title"] == "P1: Replace the CMOS battery", r["title"])

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'test_adopt: all passed'}")
sys.exit(1 if fails else 0)
