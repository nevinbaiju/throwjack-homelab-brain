"""POST /homelab and the promote / ignore / restore buttons. Scratch BRAIN_ROOT."""
import asyncio, os, sys, json

assert os.environ["BRAIN_ROOT"].startswith("/tmp/"), "refusing to run against a real brain root"
sys.path.insert(0, "/app")
import store, triage, board as bd, caldav
triage.project_to_board = lambda c, t: None
bd.write_task = lambda c, r, **k: None
bd.remove_task = lambda c, r: None
import app as A

fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + str(detail)}")
    if not ok: fails.append(label)

TOK = f"Bearer {os.environ['BRAIN_TOKEN']}"
run = asyncio.new_event_loop().run_until_complete

class Req:
    def __init__(self, body, ctype="application/json"):
        self._b = body; self.headers = {"content-type": ctype}
    async def json(self): return self._b
    async def body(self): return self._b if isinstance(self._b, bytes) else b""

store.connect().close()

# --- filing --------------------------------------------------------------
out = run(A.homelab_add(Req({"title": "Replace the CMOS battery",
                             "note": "resume session abc123", "key": "s-abc123"}), TOK))
tid = out["id"]
check("an item can be filed", out["ok"] and tid, str(out))
check("a provenance capture is written too", bool(out.get("capture")), str(out))

conn = store.connect()
r = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
check("it lands in the homelab lane", r["lane"] == "homelab", r["lane"])
check("the title is kept verbatim", r["title"] == "Replace the CMOS battery")
check("the note is kept", r["note"] == "resume session abc123")
check("it has a consequence so it does not score 0", r["consequence"] == "improves")
check("the idempotence key is stored", r["source_key"] == "homelab:s-abc123")
cap = conn.execute("SELECT * FROM captures WHERE id=?", (out["capture"],)).fetchone()
check("the capture is already closed, so triage never picks it up",
      cap["state"] == "done", cap["state"])
check("...and points at the task", cap["task_id"] == tid)

# The follow-ups timer re-reads a conversation every 30 minutes.
again = run(A.homelab_add(Req({"title": "Replace the CMOS battery (2032)",
                               "note": "resume session abc123", "key": "s-abc123"}), TOK))
check("re-filing the same key does NOT duplicate", again["id"] == tid, str(again))
check("...it updates in place", again.get("updated") is True, str(again))
check("the new title took", conn.execute(
    "SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()[0].endswith("(2032)"))
check("still exactly one row for that key", conn.execute(
    "SELECT COUNT(*) c FROM tasks WHERE source_key='homelab:s-abc123'").fetchone()["c"] == 1)

# Plain text, for a caller that does not want to build JSON.
out2 = run(A.homelab_add(Req(b"Sort out the array spindown\nhalf-done in .env",
                             ctype="text/plain"), TOK))
r2 = conn.execute("SELECT * FROM tasks WHERE id=?", (out2["id"],)).fetchone()
check("plain text works: first line is the title",
      r2["title"] == "Sort out the array spindown", r2["title"])
check("...and the rest is the note", r2["note"] == "half-done in .env", r2["note"])

for bad, why in (({"title": ""}, "an empty title"),
                 ({"title": "x", "track": "nonsense"}, "an unknown track")):
    try:
        run(A.homelab_add(Req(bad), TOK)); check(f"{why} is refused", False)
    except A.HTTPException as e:
        check(f"{why} is refused", e.status_code == 400, e.detail)

# --- promote -------------------------------------------------------------
out = run(A.homelab_promote(tid, TOK))
check("promote moves it to backlog", out["lane"] == "backlog", str(out))
check("...in the database", conn.execute(
    "SELECT lane FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "backlog")
try:
    run(A.homelab_promote("t_nope", TOK)); check("promoting an unknown id 404s", False)
except A.HTTPException as e:
    check("promoting an unknown id 404s", e.status_code == 404)

# --- ignore and undo ----------------------------------------------------
third = run(A.homelab_add(Req({"title": "Dead-man's switch for monitoring",
                               "key": "s-mon"}), TOK))["id"]
out = run(A.homelab_ignore(third, TOK))
check("ignore archives it", conn.execute(
    "SELECT archived FROM tasks WHERE id=?", (third,)).fetchone()[0] == 1)
check("...and says undo is available", out.get("undo") is True, str(out))
check("the row still exists -- nothing is deleted", conn.execute(
    "SELECT COUNT(*) c FROM tasks WHERE id=?", (third,)).fetchone()["c"] == 1)

# The agent must not resurrect something you decided against.
re_post = run(A.homelab_add(Req({"title": "Dead-man's switch for monitoring",
                                 "key": "s-mon"}), TOK))
check("re-filing an IGNORED key does not bring it back",
      re_post.get("ignored") is True, str(re_post))
check("...and it stays archived", conn.execute(
      "SELECT archived FROM tasks WHERE id=?", (third,)).fetchone()[0] == 1)

out = run(A.homelab_restore(third, TOK))
check("restore brings it back", conn.execute(
    "SELECT archived,lane FROM tasks WHERE id=?", (third,)).fetchone()[0] == 0)
check("...into the queue, not the backlog", out["lane"] == "homelab", str(out))
try:
    run(A.homelab_restore(third, TOK))
    check("restoring something already live is refused", False)
except A.HTTPException as e:
    check("restoring something already live is refused", e.status_code == 409, e.detail)

# --- board.json ---------------------------------------------------------
run(A.homelab_ignore(third, TOK))
d = run(A.board_json(TOK))
check("the lane appears in the board order", "homelab" in d["order"], str(d["order"]))
check("it sits between today and backlog",
      d["order"].index("homelab") == 1, str(d["order"]))
check("ignored items are exposed for the undo list",
      third in {i["id"] for i in d["ignored"]}, str(d["ignored"]))
check("an ignored item is NOT in the lane itself",
      third not in {t["id"] for t in d["lanes"].get("homelab", [])})
check("the promoted one shows in backlog",
      tid in {t["id"] for t in d["lanes"].get("backlog", [])})

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'test_homelab_endpoints: all passed'}")
sys.exit(1 if fails else 0)
