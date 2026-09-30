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
# A leftover from the retired iOS-owned Upkeep list. None should remain, but the
# refusal stays: a u_* id is not a brain task and never had a row of its own.
mk("u_1", "Water plants", "backlog", "upkeep")
# An ORDINARY capture that triage filed on the upkeep track -- "take the bins
# out". The old predicate was `id.startswith("u_") or track == "upkeep"`, so every
# one of these was silently unmutable AND filtered out of the planner's context.
mk("t_bins", "Take the bins out", "backlog", "upkeep")

def v(raw):
    return plan.validate(conn, raw)

p, r = v("not json at all");                              check("non-JSON is refused", not p["today"] and r)
p, r = v('```json\n{"today":[{"task_id":"t_a","why":"w"}],"mutations":[]}\n```')
check("fenced JSON is unwrapped", len(p["today"]) == 1)
p, r = v('{"today":[{"task_id":"t_ZZZ","why":"x"}],"mutations":[]}')
check("unknown task id is refused", not p["today"] and r)
p, r = v('{"today":[{"task_id":"u_1","why":"sunday"}],"mutations":[]}')
check("a task in today is not flagged as upkeep unless it is an instance",
      len(p["today"]) == 1 and not p["today"][0]["is_upkeep"])
p, r = v('{"today":[],"mutations":[{"op":"set_lane","task_id":"u_1","lane":"backlog"}]}')
check("a retired u_* chore may NOT be mutated", not p["mutations"] and r)
p, r = v('{"today":[],"mutations":[{"op":"retitle","task_id":"t_bins","title":"Bins out Tuesday"}]}')
check("an upkeep-TRACK task with no source_key IS mutable", len(p["mutations"]) == 1, str(r))
p, r = v('{"today":[{"task_id":"t_bins","why":"bin day"}],"mutations":[]}')
check("an upkeep-track task may be today's work", len(p["today"]) == 1, str(r))

# A real chore instance: created by the upkeep engine for one day.
conn.execute("INSERT INTO tasks (id,title,note,track,lane,status,score,consequence,"
  "source_key,scheduled_at,created_at,updated_at,touched_at) VALUES "
  "('t_inst','Laundry','','upkeep','today','needs_action',40,'costs',?,?,?,?,?)",
  (f"upkeep:laundry:{store.datetime.now():%Y-%m-%d}",
   f"{store.datetime.now():%Y-%m-%d}T19:00:00", now, now, now))
p, r = v('{"today":[{"task_id":"t_inst","why":"pile"}],"mutations":[]}')
check("a scheduled chore instance IS flagged as upkeep",
      len(p["today"]) == 1 and p["today"][0]["is_upkeep"], str(p["today"]))
p, r = v('{"today":[],"mutations":[{"op":"complete","task_id":"t_inst"}]}')
check("an instance MAY be completed", len(p["mutations"]) == 1, str(r))
for op, extra in (("set_lane", ',"lane":"backlog"'), ("retitle", ',"title":"X"'),
                  ("note_append", ',"text":"hi"'), ("set_blocked", ',"blocked_on":"me"')):
    p, r = v('{"today":[],"mutations":[{"op":"%s","task_id":"t_inst"%s}]}' % (op, extra))
    check(f"an instance may NOT be {op}", not p["mutations"] and r, str(r))
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
