"""Apply path, SCRATCH BRAIN_ROOT only."""
import asyncio, datetime, json, os, sys
assert os.environ["BRAIN_ROOT"].startswith("/tmp/"), "refusing to run against a real brain root"
sys.path.insert(0, "/app")
import store, plan, triage, board as bd, project
triage.project_to_board = lambda c, t: None
bd.remove_task = lambda c, r: None
import app as A
A.run_triage_once = lambda: None          # no board push in a test

class BG:                                  # stand-in for BackgroundTasks
    def add_task(self, *a, **k): pass

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        fails.append(label)

TOK = f"Bearer {os.environ['BRAIN_TOKEN']}"
today = datetime.date.today().isoformat()
conn = store.connect(); now = store.now_iso()
def mk(tid, title, lane):
    conn.execute("INSERT INTO tasks (id,title,note,track,lane,status,score,consequence,"
      "created_at,updated_at,touched_at) VALUES (?,?,'',?,?,'needs_action',40,'costs',?,?,?)",
      (tid, title, "focus", lane, now, now, now))
for tid, t, l in (("t_1","Pick me","backlog"), ("t_2","Do not pick me","backlog"),
                  ("t_3","Retitle me","in_progress"), ("t_4","Move me","in_progress")):
    mk(tid, t, l)

raw = json.dumps({"today":[{"task_id":"t_1","why":"yes"},{"task_id":"t_2","why":"no"}],
  "mutations":[{"op":"retitle","task_id":"t_3","title":"Retitled"},
               {"op":"set_lane","task_id":"t_4","lane":"backlog"},
               {"op":"add","title":"Brand new","lane":"today","consequence":"breaks"}],
  "notes":"n"})
conn.execute("INSERT INTO plan_jobs (id,kind,state,context,raw,proposal,rejected,created_at,updated_at)"
  " VALUES ('pj_a','draft','done','{}',?,?,'[]',?,?)",
  (raw, json.dumps({"today":[],"mutations":[]}), today+"T09:00:00", today+"T09:00:00"))
conn.commit()

d = asyncio.run(A.plan_today_json(authorization=TOK, session=None))
keys = {m["key"] for m in d["pending"]["proposal"]["mutations"]}
check("keys offered", sorted(keys), ["add:Brand new", "retitle:t_3", "set_lane:t_4"])

# Tick: one today pick, the retitle, the new task. Untick t_2 and the set_lane.
accept = ["t_1", "retitle:t_3", "add:Brand new"]
res = asyncio.run(A.plan_apply({"job":"pj_a","accept":accept}, BG(), authorization=TOK, session=None))
print(f"  applied: {res['today']} today, {res['mutations']} mutations, stale={res['stale']}")

def lane(tid): 
    r = conn.execute("SELECT lane,title FROM tasks WHERE id=?", (tid,)).fetchone()
    return (r["lane"], r["title"]) if r else None
check("ticked today pick moved",      lane("t_1")[0], "today")
check("UNticked today pick untouched",lane("t_2")[0], "backlog")
check("ticked retitle applied",       lane("t_3")[1], "Retitled")
check("UNticked set_lane NOT applied",lane("t_4")[0], "in_progress")
check("ticked add created in today",
      conn.execute("SELECT lane FROM tasks WHERE title='Brand new'").fetchone()["lane"], "today")
check("job marked applied",
      conn.execute("SELECT applied_at IS NOT NULL x FROM plan_jobs WHERE id='pj_a'").fetchone()["x"], 1)

d2 = asyncio.run(A.plan_today_json(authorization=TOK, session=None))
check("nothing pending after accepting", d2["pending"], None)
check("day is now current", d2["is_current"], True)

try:
    asyncio.run(A.plan_apply({"job":"pj_a","accept":accept}, BG(), authorization=TOK, session=None))
    check("re-applying is refused", "allowed", "refused")
except Exception as e:
    check("re-applying is refused", "refused", "refused")

print()
print("  FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
