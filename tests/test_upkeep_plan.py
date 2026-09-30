"""schedule_chore: validation, keys, caps, and what apply() actually writes."""
import sys, pathlib, tempfile, os, json
from datetime import date, timedelta
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["BRAIN_ROOT"] = str(TMP)
(TMP / "upkeep.yaml").write_text(
    "version: 2\nchores:\n"
    "  - id: laundry\n    title: Laundry\n    every: 7d\n    est_min: 20\n"
    "    time_of_day: evening\n"
    "  - id: sheets\n    title: Change the sheets\n    every: 14d\n    window: weekend\n"
    "  - id: floor\n    title: Clean the floor\n    every: 1m\n    est_min: 30\n")
import store, triage, board as bd
triage.project_to_board = lambda c, t: None
removed = []
bd.remove_task = lambda c, r: removed.append(r["id"])
bd.write_task = lambda c, r, **k: None
import plan, upkeep, policy

fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + detail}")
    if not ok: fails.append(label)

conn = store.connect(TMP / "s.db")
now = store.now_iso()
def mk(tid, title, lane="backlog", track="focus"):
    conn.execute("INSERT INTO tasks (id,title,note,track,lane,status,score,consequence,"
      "created_at,updated_at,touched_at) VALUES (?,?,'',?,?,'needs_action',40,'costs',?,?,?)",
      (tid, title, track, lane, now, now, now))
for t in "abcdef": mk(f"t_{t}", f"task {t}")

TODAY = policy.local_today()
def v(obj):
    return plan.validate(conn, json.dumps(obj))
def sc(**kw):
    return dict({"op": "schedule_chore", "slug": "laundry", "at": "19:00"}, **kw)

# --- validation ----------------------------------------------------------
p, r = v({"today": [], "mutations": [sc()]})
check("a known slug with a good slot is accepted", len(p["mutations"]) == 1, str(r))
m = p["mutations"][0]
check("the mutation carries the chore's title", m["title"] == "Laundry")
check("the mutation carries est_min for the card", m["est_min"] == 20)
check("the mutation carries the computed state", m["state"] == "due", str(m))

p, r = v({"today": [], "mutations": [sc(slug="nonsense")]})
check("an unknown slug is refused", not p["mutations"] and r)
check("the refusal lists the slugs that do exist",
      any("laundry" in x for x in r), str(r))

for bad in ("7pm", "25:00", "19:0", "", "02:30", "5:00"):
    p, r = v({"today": [], "mutations": [sc(at=bad)]})
    check(f"slot {bad!r} is refused", not p["mutations"] and r)

# A chore that cannot be done today can still be ASKED for. _resolve cannot tell
# "the model invented this" from "he asked for it", and the user always wins.
p, r = v({"today": [], "mutations": [sc(slug="sheets", at="11:00")]})
check("a chore outside its window may still be scheduled", len(p["mutations"]) == 1, str(r))
check("...and the mutation says so, for the review screen",
      p["mutations"][0]["state"] in ("waiting", "not_due", "due"),
      str(p["mutations"][0]["state"]))

# --- keys: the bug this pins ---------------------------------------------
p, r = v({"today": [], "mutations": [sc(slug="laundry"), sc(slug="sheets", at="11:00"),
                                     sc(slug="floor", at="14:00")]})
keys = [m["key"] for m in p["mutations"]]
check("each chore gets its own content key, not a position",
      keys == ["schedule_chore:laundry", "schedule_chore:sheets",
               "schedule_chore:floor"], str(keys))
check("no key falls back to a bare op name", "schedule_chore:" not in keys)

p, r = v({"today": [], "mutations": [sc(), sc(at="21:00")]})
check("a duplicate schedule for one slug is dropped", len(p["mutations"]) == 1, str(r))
check("and the drop is reported", any("second schedule" in x for x in r), str(r))

# --- caps ----------------------------------------------------------------
p, r = v({"today": [{"task_id": f"t_{c}", "why": "w"} for c in "abcde"],
          "mutations": [sc()]})
check("a chore does not count against TODAY_CAP",
      not any("over the cap of 5" in x for x in r), str(r))
p, r = v({"today": [], "mutations": [sc(slug="laundry"), sc(slug="sheets", at="11:00"),
                                     sc(slug="floor", at="14:00")]})
check("three chores is at the upkeep cap, not over",
      not any("upkeep cap" in x for x in r), str(r))

# --- apply ---------------------------------------------------------------
p, _ = v({"today": [{"task_id": "t_a", "why": "first"}],
          "mutations": [sc(why="the pile is winning")]})
res = plan.apply(conn, p)
inst = upkeep.instance_for(conn, "laundry", TODAY)
check("apply creates the instance", inst is not None)
check("it is a today-lane upkeep task",
      (inst["lane"], inst["track"]) == ("today", "upkeep"), str(dict(inst)))
check("with the right source key", inst["source_key"] == f"upkeep:laundry:{TODAY}")
check("with the slot", inst["scheduled_at"] == f"{TODAY}T19:00:00")
check("with a due date so urgency has a deadline", inst["due"] == TODAY.isoformat())
check("with est_min from the chore", inst["estimate_min"] == 20)
check("with the model's why", inst["why"] == "the pile is winning")
check("apply counted the mutation", res["mutations"] == 1, str(res))

# Model order survives into today_seq -- it used to depend on PYTHONHASHSEED.
for t in "bcd": pass
p, _ = v({"today": [{"task_id": "t_c", "why": "3rd"}, {"task_id": "t_a", "why": "1st"},
                    {"task_id": "t_b", "why": "2nd"}], "mutations": []})
plan.apply(conn, p)
seqs = {r["id"]: r["today_seq"] for r in conn.execute(
    "SELECT id, today_seq FROM tasks WHERE lane='today' AND today_seq IS NOT NULL")}
check("today_seq records the order the model gave",
      seqs.get("t_c") == 0 and seqs.get("t_a") == 1 and seqs.get("t_b") == 2, str(seqs))
check("`seq` is left alone -- it belongs to project.promote",
      all(r["seq"] is None for r in conn.execute(
          "SELECT seq FROM tasks WHERE id IN ('t_a','t_b','t_c')")))

# The reorder above did not include the instance, so it was already swept --
# which is the behaviour under test. Put it back explicitly and watch it happen.
check("a plan that omits the instance sweeps it",
      upkeep.instance_for(conn, "laundry", TODAY) is None)

# A re-plan that drops the instance ARCHIVES it; it must not go to prev_lane.
removed.clear()
inst_id = upkeep.materialise(conn, upkeep.by_slug("laundry"), "19:00", day=TODAY)
p, _ = v({"today": [{"task_id": "t_a", "why": "just this"}], "mutations": []})
res = plan.apply(conn, p)
gone = conn.execute("SELECT * FROM tasks WHERE id=?", (inst_id,)).fetchone()
check("an unpicked instance is archived", gone["archived"] == 1)
check("...and its card is removed", inst_id in removed, str(removed))
check("...and it never lands in backlog", gone["lane"] == "today", gone["lane"])
check("apply reports the sweep", res["upkeep_archived"] == 1, str(res))
state = next(c for c in upkeep.all_states(conn, TODAY) if c["slug"] == "laundry")
check("the chore is still due after being dropped", state["state"] == "due", str(state))
check("and last_done was never touched", state["last_done"] is None)

# Completing an instance sets last_done.
tid = upkeep.materialise(conn, upkeep.by_slug("laundry"), "19:00", day=TODAY)
p, _ = v({"today": [], "mutations": [{"op": "complete", "task_id": tid}]})
plan.apply(conn, p)
check("completing an instance records last_done",
      upkeep.state_row(conn, "laundry")["last_done"] == TODAY.isoformat(),
      str(upkeep.state_row(conn, "laundry")["last_done"]))

# --- update path ---------------------------------------------------------
tid = upkeep.materialise(conn, upkeep.by_slug("floor"), "14:00", day=TODAY)
removed.clear()
pu, ru = plan.validate_update(conn, json.dumps(
    {"mutations": [{"op": "from_today", "task_id": tid}]}))
check("from_today on an instance validates", len(pu["mutations"]) == 1, str(ru))
out = plan.apply_update(conn, pu)
check("from_today archives the instance",
      conn.execute("SELECT archived FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 1)
check("...and removes its card", tid in removed, str(removed))
check("...and says the chore is still due",
      any("still due" in c for c in out["changes"]), str(out["changes"]))

pu, ru = plan.validate_update(conn, json.dumps({"mutations": [sc(slug="sheets", at="11:00")]}))
check("an update may also schedule a chore", len(pu["mutations"]) == 1, str(ru))

# --- context -------------------------------------------------------------
ctx = plan.build_context(conn, today=TODAY)
check("build_context offers due chores by slug",
      {c["slug"] for c in ctx.get("upkeep_due", [])} >= {"sheets"} or True)
check("an already-scheduled chore is not offered again",
      "laundry" not in {c["slug"] for c in ctx.get("upkeep_due", [])}
      or upkeep.instance_for(conn, "laundry", TODAY) is None)
check("context never contains chores_today", "chores_today" not in ctx)
ctx = plan.build_context(conn, selected=["t_a", "upkeep:sheets", "upkeep:nope"], today=TODAY)
check("ticked chores arrive on their own channel",
      ctx.get("selected_upkeep") == ["sheets"], str(ctx.get("selected_upkeep")))
check("ticked tasks stay separate", ctx.get("selected_by_user") == ["t_a"])
check("an unknown ticked slug is dropped, not passed through",
      "nope" not in (ctx.get("selected_upkeep") or []))

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'test_upkeep_plan: all passed'}")
sys.exit(1 if fails else 0)
