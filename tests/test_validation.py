"""What validate() must refuse. Each line here is a bug that reached the board."""
import json, sys, tempfile, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import store, triage, board as bd
triage.project_to_board = lambda c, t: None
bd.remove_task = lambda c, r: None
import plan

fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + detail}")
    if not ok: fails.append(label)

conn = store.connect(pathlib.Path(tempfile.mkdtemp()) / "s.db")
now = store.now_iso()
def mk(tid, title, lane="backlog", track="focus"):
    conn.execute("INSERT INTO tasks (id,title,note,track,lane,status,score,consequence,"
      "created_at,updated_at,touched_at) VALUES (?,?,'',?,?,'needs_action',40,'costs',?,?,?)",
      (tid, title, track, lane, now, now, now))
for t in "abcdef": mk(f"t_{t}", f"task {t}")
mk("u_1", "Water plants", "upkeep", "upkeep")

def v(raw):
    return plan.validate(conn, raw)

p, r = v("not json at all");                              check("non-JSON is refused", not p["today"] and r)
p, r = v('```json\n{"today":[{"task_id":"t_a","why":"w"}],"mutations":[]}\n```')
check("fenced JSON is unwrapped", len(p["today"]) == 1)
p, r = v('{"today":[{"task_id":"t_ZZZ","why":"x"}],"mutations":[]}')
check("unknown task id is refused", not p["today"] and r)
p, r = v('{"today":[{"task_id":"u_1","why":"sunday"}],"mutations":[]}')
check("a chore MAY be today's work", len(p["today"]) == 1 and p["today"][0]["is_chore"])
p, r = v('{"today":[],"mutations":[{"op":"set_lane","task_id":"u_1","lane":"backlog"}]}')
check("a chore may NOT be mutated", not p["mutations"] and r)
p, r = v('{"today":[],"mutations":[{"op":"set_lane","task_id":"t_b","lane":"today"}]}')
check("set_lane cannot smuggle a task into today", not p["mutations"] and r)
p, r = v('{"today":[],"mutations":[{"op":"delete","task_id":"t_b"}]}')
check("an unknown op is refused", not p["mutations"] and r)
p, r = v('{"today":[],"mutations":[{"op":"retitle","task_id":"t_b","title":"  "}]}')
check("an empty retitle is refused", not p["mutations"] and r)
p, r = v('{"today":[],"mutations":[{"op":"list_add","list":"shop-nope","items":["x"]}]}')
check("an unknown shopping list is refused", not p["mutations"] and r)
p, r = v('{"today":[],"mutations":[{"op":"add","title":"New","lane":"today"}]}')
check("add with no consequence lands mid-scale, not at the floor",
      p["mutations"] and p["mutations"][0]["consequence"] == "costs")
p, r = v(json.dumps({"today":[{"task_id":f"t_{x}","why":"w"} for x in "abcdef"],"mutations":[]}))
check("the cap warns but never truncates", len(p["today"]) == 6 and r)
p, r = v('{"today":[],"mutations":[{"op":"complete","task_id":"t_a"}]}')
check("a draft may mark something already done", len(p["mutations"]) == 1)

print()
print("  FAILURES:", fails or "none")
sys.exit(1 if fails else 0)
