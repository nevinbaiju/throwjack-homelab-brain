"""Endpoint-level test against a SCRATCH BRAIN_ROOT. Never the live database."""
import asyncio, datetime, json, os, sys

assert os.environ["BRAIN_ROOT"].startswith("/tmp/"), "refusing to run against a real brain root"
sys.path.insert(0, "/app")
import store, plan, triage, board as bd, project
triage.project_to_board = lambda c, t: None
bd.remove_task = lambda c, r: None
import app as A

fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        fails.append(label)

TOK = f"Bearer {os.environ['BRAIN_TOKEN']}"
today = datetime.date.today().isoformat()
old   = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()

conn = store.connect(); now = store.now_iso()
def mk(tid, title, lane):
    conn.execute("INSERT INTO tasks (id,title,note,track,lane,status,score,consequence,"
      "created_at,updated_at,touched_at) VALUES (?,?,'',?,?,'needs_action',40,'costs',?,?,?)",
      (tid, title, "focus", lane, now, now, now))
mk("t_p", "Another backlog thing", "backlog")
mk("t_q", "Backlog thing", "backlog")
mk("t_r", "Carried from before", "today")
conn.execute("UPDATE tasks SET prev_lane='backlog' WHERE id='t_r'")
conn.commit()

def get():
    return asyncio.run(A.plan_today_json(authorization=TOK, session=None))

print("== fresh day, nothing pending ==")
d = get()
check("is_current", d["is_current"], False)
check("pending", d["pending"], None)
check("carried item present", [t["title"] for t in d["today"]], ["Carried from before"])
check("carried is labelled", d["today"][0]["carried_from"] is not None, True)
# in_progress was merged into today, so the pool is backlog only. Today is the
# single active lane and its contents are reported as `today`, not as pool.
check("pool is backlog only", sorted(set(t["lane"] for t in d["pool"])), ["backlog"])

print("\n== a request still in flight is reported, not hidden ==")
conn.execute("INSERT INTO plan_jobs (id,kind,state,context,created_at,updated_at)"
  " VALUES ('pj_run','draft','running','{}',?,?)", (today+"T09:30:00", today+"T09:30:00"))
conn.commit()
d = get()
check("working reported", d["working"]["id"], "pj_run")
check("pending still none while it runs", d["pending"], None)
conn.execute("UPDATE plan_jobs SET state='failed' WHERE id='pj_run'"); conn.commit()
check("a failed job is not 'working'", get()["working"], None)

print("\n== a STALE draft must not surface, and must be retired ==")
raw = json.dumps({"today":[{"task_id":"t_q","why":"do it"}],
                  "mutations":[{"op":"set_lane","task_id":"t_p","lane":"backlog"}],
                  "notes":"prose written five days ago"})
conn.execute("INSERT INTO plan_jobs (id,kind,state,context,raw,proposal,rejected,created_at,updated_at)"
  " VALUES ('pj_stale','draft','done','{}',?,?,'[]',?,?)",
  (raw, json.dumps({"today":[],"mutations":[]}), old+"T10:00:00", old+"T10:00:00"))
conn.commit()
d = get()
check("not surfaced", d["pending"], None)
check("retired in the database",
      conn.execute("SELECT dismissed_at IS NOT NULL x FROM plan_jobs WHERE id='pj_stale'").fetchone()["x"], 1)

print("\n== a draft from TODAY surfaces, re-validated ==")
conn.execute("INSERT INTO plan_jobs (id,kind,state,context,raw,proposal,rejected,message,created_at,updated_at)"
  " VALUES ('pj_today','draft','done','{}',?,?,'[]','plan my day',?,?)",
  (raw, json.dumps({"today":[],"mutations":[]}), today+"T09:00:00", today+"T09:00:00"))
conn.commit()
d = get()
check("surfaced", d["pending"]["id"], "pj_today")
check("from_today", d["pending"]["from_today"], True)
check("message carried", d["pending"]["message"], "plan my day")
check("re-validated today pick", [i["task_id"] for i in d["pending"]["proposal"]["today"]], ["t_q"])
check("mutation has a stable key",
      [m["key"] for m in d["pending"]["proposal"]["mutations"]], ["set_lane:t_p"])
check("nothing dropped yet", d["pending"]["dropped"], 0)
check("notes shown while nothing is stale",
      d["pending"]["proposal"]["notes"], "prose written five days ago")

print("\n== complete a referenced task: it drops, and the prose is withheld ==")
project.set_state(conn, "t_q", "done"); conn.commit()
d = get()
check("today pick dropped", d["pending"]["proposal"]["today"], [])
check("dropped counted", d["pending"]["dropped"], 1)
check("notes withheld", d["pending"]["proposal"]["notes"], "")
check("surviving mutation intact",
      [m["key"] for m in d["pending"]["proposal"]["mutations"]], ["set_lane:t_p"])

print()
print("  FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
