"""Chore endpoints, against a SCRATCH BRAIN_ROOT. Never the live database."""
import asyncio, datetime, json, os, sys

assert os.environ["BRAIN_ROOT"].startswith("/tmp/"), "refusing to run against a real brain root"
sys.path.insert(0, "/app")
import store, triage, board as bd, caldav, upkeep
triage.project_to_board = lambda c, t: None
bd.remove_task = lambda c, r: None
bd.write_task = lambda c, r, **k: None
import app as A

fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + str(detail)}")
    if not ok: fails.append(label)

TOK = f"Bearer {os.environ['BRAIN_TOKEN']}"
run = asyncio.get_event_loop().run_until_complete
TODAY = datetime.date.today().isoformat()

class Req:
    def __init__(self, body): self._b = body
    async def json(self): return self._b

store.connect().close()          # create the scratch schema
upkeep.CHORES_FILE.write_text("version: 2\nchores: []\n")
upkeep._cache = None

# --- create ---------------------------------------------------------------
out = run(A.upkeep_create(Req({"title": "Change the sheets", "every": "14d",
                               "window": "weekend", "est_min": 15}), TOK))
check("a chore can be created", out["slug"] == "change-the-sheets", str(out))
out = run(A.upkeep_create(Req({"title": "Laundry", "every": "7d",
                               "time_of_day": "evening", "est_min": 20}), TOK))
check("a second chore can be created", out["slug"] == "laundry", str(out))

# The words a person types, which used to 400 with an invisible error message.
out = run(A.upkeep_create(Req({"title": "Clean the floor", "every": "monthly"}), TOK))
check("'monthly' is accepted and stored canonically", out["slug"] == "clean-the-floor")
upkeep._cache = None
check("...as 1m", upkeep.by_slug("clean-the-floor")["every"] == "1m",
      upkeep.by_slug("clean-the-floor")["every"])
run(A.upkeep_delete("clean-the-floor", TOK))
upkeep._cache = None

for bad, why in (({"title": "", "every": "7d"},           "no title"),
                 ({"title": "X"},                          "no cadence"),
                 ({"title": "X", "every": "sometimes"},    "an unreadable cadence"),
                 ({"title": "X", "every": "7d", "window": "fortnight"}, "a bad window"),
                 ({"title": "X", "every": "7d", "days": ["funday"]},    "a bad weekday")):
    try:
        run(A.upkeep_create(Req(bad), TOK))
        check(f"{why} is rejected", False)
    except A.HTTPException as e:
        check(f"{why} is rejected with a readable message",
              e.status_code == 400 and len(str(e.detail)) > 10, e.detail)
try:
    run(A.upkeep_create(Req({"title": "Laundry", "every": "7d"}), TOK))
    check("a duplicate chore is rejected", False)
except A.HTTPException as e:
    check("a duplicate chore is rejected", e.status_code == 409, e.detail)

# --- /lists.json does NOT need CalDAV for the upkeep half -----------------
def boom(*a, **k):
    raise caldav.CalDavError("radicale is down")
caldav.list_todos = boom
caldav.list_collections = lambda: []
data = run(A.lists_json(TOK))
slugs = {c["slug"] for c in data["upkeep"]}
check("the Upkeep tab renders with CalDAV dead",
      slugs == {"laundry", "change-the-sheets"}, str(slugs))
check("and reports no errors", data["upkeep_errors"] == [], str(data["upkeep_errors"]))
check("every chore carries its state",
      all(c.get("state") for c in data["upkeep"]), str(data["upkeep"]))

# --- done -----------------------------------------------------------------
out = run(A.upkeep_done("laundry", Req({}), TOK))
check("marking a chore done sets last_done", out["last_done"] == TODAY, str(out))
data = run(A.lists_json(TOK))
laundry = next(c for c in data["upkeep"] if c["slug"] == "laundry")
check("a chore just done is no longer due", laundry["state"] == "not_due", str(laundry))
try:
    run(A.upkeep_done("nope", Req({}), TOK)); check("done on an unknown slug 404s", False)
except A.HTTPException as e:
    check("done on an unknown slug 404s", e.status_code == 404)

# --- schedule -------------------------------------------------------------
out = run(A.upkeep_schedule("change-the-sheets", Req({"at": "11:00"}), TOK))
check("a chore can be scheduled by hand", out["scheduled_at"] == f"{TODAY}T11:00:00", str(out))
conn = store.connect()
inst = upkeep.instance_for(conn, "change-the-sheets", datetime.date.today())
check("the instance is in today", inst is not None and inst["lane"] == "today")
check("it is not offered for scheduling twice",
      not any(c["slug"] == "change-the-sheets"
              for c in run(A.plan_today_json(TOK))["upkeep_due"]))
try:
    run(A.upkeep_schedule("laundry", Req({"at": "7pm"}), TOK))
    check("a bad slot is rejected", False)
except A.HTTPException as e:
    check("a bad slot is rejected with the reason", e.status_code == 400, e.detail)

# --- today.json -----------------------------------------------------------
d = run(A.plan_today_json(TOK))
check("today.json exposes upkeep_due", "upkeep_due" in d)
check("today.json exposes upkeep_waiting", "upkeep_waiting" in d)
inst_row = next((t for t in d["today"] if t["is_upkeep"]), None)
check("the instance appears in today, flagged", inst_row is not None, str(d["today"]))
check("with its slot", inst_row and inst_row["at"] == "11:00", str(inst_row))
check("and is never labelled carried_from", inst_row and inst_row["carried_from"] is None)

# --- snooze ---------------------------------------------------------------
out = run(A.upkeep_snooze("laundry", Req({"days": 5}), TOK))
check("a chore can be snoozed", out["snooze_until"] > TODAY, str(out))
laundry = next(c for c in run(A.lists_json(TOK))["upkeep"] if c["slug"] == "laundry")
check("a snoozed chore says so", laundry["state"] in ("snoozed", "not_due"), str(laundry))

# --- cadence --------------------------------------------------------------
out = run(A.upkeep_cadence("laundry", Req({"every": "10d", "window": "weekend"}), TOK))
check("cadence can be changed", out["every"] == "10d", str(out))
upkeep._cache = None
c = upkeep.by_slug("laundry")
check("the new window is stored", c["days_set"] == upkeep.WEEKEND, str(c))
check("the id is unchanged by an edit", c["slug"] == "laundry")
out = run(A.upkeep_cadence("laundry", Req({"title": "Do the washing"}), TOK))
upkeep._cache = None
check("renaming keeps the id, so last_done survives",
      upkeep.by_slug("laundry")["title"] == "Do the washing")
check("...and last_done really did survive",
      upkeep.state_row(store.connect(), "laundry")["last_done"] == TODAY)
try:
    run(A.upkeep_cadence("laundry", Req({"every": "sometimes"}), TOK))
    check("an unusable cadence is rejected", False)
except A.HTTPException as e:
    check("an unusable cadence is rejected", e.status_code == 400, e.detail)

# --- delete ---------------------------------------------------------------
run(A.upkeep_delete("laundry", TOK))
upkeep._cache = None
check("a deleted chore is gone from the list",
      [c["slug"] for c in upkeep.load()] == ["change-the-sheets"])
check("...and does not come back from its surviving state row",
      "laundry" not in {c["slug"] for c in run(A.lists_json(TOK))["upkeep"]})
check("...though the state row is still there, unarchived",
      upkeep.state_row(store.connect(), "laundry") is not None)

# --- retire is safe to dry-run -------------------------------------------
out = run(A.upkeep_retire(True, TOK))
check("retire dry-runs without touching anything", out.get("ok") is True, str(out))

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'test_upkeep_endpoints: all passed'}")
sys.exit(1 if fails else 0)
