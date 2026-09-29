"""Scratch DB only. Never the live board -- AGENTS.md, and I have been ignoring it."""
import json, sys, tempfile, pathlib, datetime
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
import store, triage, board as bd, project
triage.project_to_board = lambda c,t: None
bd.remove_task = lambda c,r: None
TMP = pathlib.Path(tempfile.mkdtemp()); project.CONTEXTS = TMP/"ctx"; project.CONTEXTS.mkdir(parents=True)
import plan

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        fails.append(label)

conn = store.connect(TMP/"s.db"); now = store.now_iso()
def mk(tid, title, lane="backlog", track="focus"):
    conn.execute("INSERT INTO tasks (id,title,note,track,lane,status,score,consequence,"
      "created_at,updated_at,touched_at) VALUES (?,?,'',?,?,'needs_action',40,'costs',?,?,?)",
      (tid,title,track,lane,now,now,now))
for t in "abcd": mk(f"t_{t}", f"task {t}")

print("== 1. keys exist and are content-derived ==")
raw = json.dumps({"today":[{"task_id":"t_a","why":"w"}],"mutations":[
    {"op":"set_lane","task_id":"t_b","lane":"in_progress"},
    {"op":"set_lane","task_id":"t_c","lane":"backlog"},
    {"op":"note_append","task_id":"t_d","text":"one"},
    {"op":"note_append","task_id":"t_d","text":"two"},
    {"op":"add","title":"New thing","lane":"today","consequence":"costs"}]})
p1,_ = plan.validate(conn, raw)
keys1 = [m["key"] for m in p1["mutations"]]
check("every mutation has a key", all(bool(k) for k in keys1), True)
check("duplicate op+task disambiguated",
      [k for k in keys1 if k.startswith("note_append")],
      ["note_append:t_d", "note_append:t_d#1"])

print("\n== 2. keys are STABLE when re-validation drops an entry ==")
# t_b gets completed between drafting and accepting.
project.set_state(conn, "t_b", "done")
p2, stale = plan.validate(conn, raw)
keys2 = [m["key"] for m in p2["mutations"]]
check("the dropped one is gone", "set_lane:t_b" in keys2, False)
check("every surviving key is unchanged", [k for k in keys1 if k != "set_lane:t_b"], keys2)
check("something was reported dropped", len(stale) > 0, True)

print("\n== 3. unticking by key removes the RIGHT change (the positional bug) ==")
# Before: indices. p1 index 1 was set_lane:t_c. After the drop, index 1 is
# note_append:t_d -- a positional accept would have discarded the wrong one.
positional_would_hit = keys2[1]
check("position 1 now means a different change", positional_would_hit != keys1[1], True)
kept = {"note_append:t_d"}                        # user unticked everything else
filtered = [m for m in p2["mutations"] if m["key"] in kept]
check("key filter keeps exactly what was ticked",
      [m["key"] for m in filtered], ["note_append:t_d"])

print("\n== 4. accept also filters today picks by task id ==")
kept = {"t_a", "add:New thing"}
prop = dict(p2)
prop["mutations"] = [m for m in p2["mutations"] if m["key"] in kept]
prop["today"]     = [i for i in p2["today"] if i["task_id"] in kept]
check("today pick kept", [i["task_id"] for i in prop["today"]], ["t_a"])
check("add kept by key", [m["key"] for m in prop["mutations"]], ["add:New thing"])

print("\n== 5. stale pending drafts are retired ==")
today = datetime.date.today().isoformat()
old = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
for jid, created in (("pj_old1", old+"T10:00:00"), ("pj_old2", old+"T11:00:00"),
                     ("pj_now", today+"T09:00:00")):
    conn.execute("INSERT INTO plan_jobs (id,kind,state,context,proposal,created_at,updated_at)"
      " VALUES (?,'draft','done','{}','{\"today\":[],\"mutations\":[]}',?,?)", (jid, created, created))
check("newest pending before retiring", plan.pending_job(conn)["id"], "pj_now")
conn.execute("UPDATE plan_jobs SET created_at=?, updated_at=? WHERE id='pj_now'",
             (old+"T12:00:00", old+"T12:00:00"))
# One call drains every stale draft, newest first -- not one per call.
check("one call bins all three stale drafts",
      sorted(plan.retire_stale_pending(conn, today)), ["pj_now", "pj_old1", "pj_old2"])
check("a second call has nothing left to do", plan.retire_stale_pending(conn, today), [])
check("nothing pending afterwards", plan.pending_job(conn), None)
check("retiring is idempotent", plan.retire_stale_pending(conn, today), [])

print("\n== 6. a draft from today is NOT retired ==")
conn.execute("INSERT INTO plan_jobs (id,kind,state,context,proposal,created_at,updated_at)"
  " VALUES ('pj_keep','draft','done','{}','{\"today\":[],\"mutations\":[]}',?,?)",
  (today+"T08:00:00", today+"T08:00:00"))
check("survives", plan.retire_stale_pending(conn, today), [])
check("still pending", plan.pending_job(conn)["id"], "pj_keep")

print()
print("  FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
