"""bin/brain-followups: conversations in, Homelab Backlog items out, nothing filed twice.

Scratch transcripts and a scratch state dir; the model call (ask) and the brain's HTTP API
are stubbed, so nothing here runs a model or reaches the real brain.
"""
import importlib.machinery, importlib.util, json, os, pathlib, sqlite3, sys, tempfile, time
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
TMP = pathlib.Path(tempfile.mkdtemp())
os.environ.update(BRAIN_DATA=str(TMP / "data"), CLAUDE_PROJECTS=str(TMP / "projects"),
                  BRAIN_TOKEN="test", BRAIN_URL="http://127.0.0.1:9")
loader = importlib.machinery.SourceFileLoader("followups", str(ROOT / "bin" / "brain-followups"))
spec = importlib.util.spec_from_loader("followups", loader)
fu = importlib.util.module_from_spec(spec)
loader.exec_module(fu)

fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + str(detail)}")
    if not ok: fails.append(label)

PROJ = TMP / "projects" / "-home-someone"
PROJ.mkdir(parents=True)

def rec(kind, text=None, **kw):
    r = {"type": kind, "isSidechain": False, "cwd": "/home/someone",
         "timestamp": "2026-10-01T12:00:00Z"}
    if text is not None:
        r["message"] = {"role": kind, "content": text}
    r.update(kw)
    return r

def write(path, records, age_h=3.0):
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    t = time.time() - age_h * 3600
    os.utime(path, (t, t))

# --- reading a transcript ---------------------------------------------------
A = PROJ / "aaaaaaaa-1111.jsonl"
write(A, [
    {"type": "ai-title", "aiTitle": "Set up the printer"},
    rec("user", "Set up the printer <system-reminder>noise</system-reminder>"),
    rec("assistant", [{"type": "text", "text": "Installing the driver."},
                      {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]),
    rec("user", [{"type": "tool_result", "content": "secret tool output"}], toolUseResult={}),
    rec("assistant", [{"type": "text", "text": "subagent chatter"}], isSidechain=True),
    rec("user", "<command-name>/compact</command-name>"),
    rec("user", "caveat", isMeta=True),
    rec("user", '<pasted_content id="x">pasted words</pasted_content>'),
    rec("assistant", [{"type": "text", "text": "Done, except the duplex test: your call."}]),
])
c = fu.read(str(A))
check("title and cwd are read", c["title"] == "Set up the printer" and c["cwd"] == "/home/someone", c)
check("only typed text and Claude's text survive",
      [w for w, _ in c["msgs"]] == ["user", "claude", "user", "claude"], c["msgs"])
check("system reminders are stripped from what was typed", c["msgs"][0][1] == "Set up the printer", c["msgs"][0])
check("a paste keeps its text and loses its tag", c["msgs"][2][1] == "pasted words", c["msgs"][2])
check("tool output never reaches the model", "secret" not in fu.render(c))
check("it is a conversation", fu.is_conversation(c))

P = PROJ / "pppppppp-2222.jsonl"
write(P, [rec("user", '{"date": "2026-10-01", "lanes": {}}'), rec("assistant", [{"type": "text", "text": "{}"}])])
check("a planner run is not a conversation", not fu.is_conversation(fu.read(str(P))))

S = PROJ / "ssssssss-3333.jsonl"
write(S, [rec("user", "old start"), rec("assistant", [{"type": "text", "text": "old reply"}]),
          rec("user", "This session is being continued...", isCompactSummary=True),
          rec("user", "carry on")])
s = fu.read(str(S))
check("a compaction summary replaces what it summarises",
      [w for w, _ in s["msgs"]] == ["summary", "user"] and s["prompt"] == "old start", s["msgs"])

fu.BUDGET, fu.PER_MESSAGE = 200, 60
big = {"msgs": [("user", "FIRST")] + [("claude", f"middle {i} " + "x" * 30) for i in range(20)]
       + [("claude", "LAST")]}
out = fu.render(big)
check("the cut keeps the start and the end", out.startswith("USER: FIRST") and out.endswith("CLAUDE: LAST"), out)
check("the cut says how much it left out", "messages left out" in out, out)
check("a long message keeps its head and tail", "[...]" in fu.render({"msgs": [("user", "a" * 100)]}))
fu.BUDGET, fu.PER_MESSAGE = 120_000, 8_000

# --- what the model proposes, code decides -----------------------------------
got = fu.clean([{"title": "Work out why DNS drops", "context": "c"},
                {"title": "p2 renew the cert", "context": "c"},
                {"title": "ok", "context": "too short to be a task"},
                {"title": "Check\nthe   logs", "context": "x" * 900}] +
               [{"title": f"Task {i}", "context": ""} for i in range(10)])
check("a leading 'Work' cannot route to the meta track", got[0]["title"] == "Follow up: Work out why DNS drops", got[0])
check("a leading P2 cannot set a priority", got[1]["title"].startswith("Follow up: p2"), got[1])
check("a title too short to act on is dropped", all(g["title"] != "ok" for g in got))
check("titles are one line, contexts bounded",
      got[2]["title"] == "Check the logs" and len(got[2]["context"]) == 600, got[2])
check("at most MAX_ITEMS per conversation", len(got) <= fu.MAX_ITEMS, len(got))
rated = fu.clean([{"title": "Rotate the leaked key", "context": "", "consequence": "breaks"},
                  {"title": "Tidy the notes", "context": "", "consequence": "whenever"}])
e = fu.entry(rated[0], {"id": "x" * 12, "cwd": None})
check("the rating goes with the item, and the title is sent as written",
      e["consequence"] == "breaks" and e["title"] == "Rotate the leaked key", e)
check("the key is stable across rewordings the dedupe already ignores",
      e["key"] == fu.entry(dict(rated[0], title="rotate the  leaked key."),
                           {"id": "x" * 12, "cwd": None})["key"], e)
check("an unknown rating falls back to improves", rated[1]["consequence"] == "improves", rated[1])

# --- run: first run, filing, rereads ------------------------------------------
posted, extracted, busy = [], [], set()
proposals = {"aaaaaaaa-1111": [{"title": "Run the duplex test", "context": "Driver is in."}]}
def fake_extract(c, state):
    extracted.append((c["id"], fu.prompt(c, state)))
    return list(proposals.get(c["id"], []))
def fake_post(e):
    posted.append(e)
    return f"c_{len(posted):012d}"
fu.ask, fu.post, fu.busy = fake_extract, fake_post, lambda: busy

check("first run returns cleanly", fu.run() == 0)
check("first run files nothing", posted == [] and extracted == [], (posted, extracted))
state = fu.load()
check("first run counts every conversation that ended as read", str(A) in state["read"], state["read"].keys())

write(A, [rec("user", "the printer jams on duplex"),
          rec("assistant", [{"type": "text", "text": "Try it after the firmware update."}])])
check("a grown, quiet conversation runs", fu.run() == 0)
check("its loose end is filed", len(posted) == 1 and posted[0]["title"] == "Run the duplex test", posted)
check("unrated, it goes in as improves", posted[0]["consequence"] == "improves", posted)
check("the note keeps the context and says where to pick it up",
      posted[0]["note"].startswith("Driver is in.\n")
      and "claude --resume aaaaaaaa-1111" in posted[0]["note"], posted)
state = fu.load()
check("what was filed is remembered", state["filed"]["aaaaaaaa-1111"][0]["capture"] == "c_000000000001", state["filed"])

extracted.clear()
fu.run()
check("nothing new, nothing read", extracted == [], extracted)

write(A, [{"type": "custom-title", "customTitle": "printer"}])
fu.run()
check("a rename alone does not reread the conversation", extracted == [], extracted)

write(A, [rec("user", "also the scanner"), rec("assistant", [{"type": "text", "text": "Later."}])])
fu.run()
check("a conversation that grew is reread", len(extracted) == 1, extracted)
check("and told what it already filed", "- Run the duplex test" in extracted[0][1], extracted[0][1][:400])
check("the same loose end is not filed twice", len(posted) == 1, posted)

extracted.clear()
write(A, [rec("user", "one more thing")], age_h=0.1)
fu.run()
check("a conversation still going is left alone", extracted == [], extracted)

write(A, [rec("user", "and another")], age_h=3)
busy = {"aaaaaaaa-1111"}
fu.run()
check("a session that is busy is left alone", extracted == [], extracted)
busy = set()

# --- failures ------------------------------------------------------------------
proposals["aaaaaaaa-1111"] = [{"title": "Order toner", "context": ""}, {"title": "Mount the shelf", "context": ""}]
def post_then_fail(e):
    if posted and posted[-1]["title"] == "Order toner":
        raise urllib.error.URLError("connection refused")
    return fake_post(e)
fu.post = post_then_fail
check("the brain being down fails the run", fu.run() == 1)
state = fu.load()
check("what got through before it went down is remembered",
      [f["title"] for f in state["filed"]["aaaaaaaa-1111"]][-1] == "Order toner", state["filed"])
check("the conversation is not marked read", state["read"][str(A)]["size"] < A.stat().st_size)
check("and no attempt is held against it", "aaaaaaaa-1111" not in state["failed"], state["failed"])
fu.post = fake_post

def broken(c, state):
    raise RuntimeError("model said nothing useful")
fu.ask = broken
codes = [fu.run() for _ in range(fu.GIVE_UP)]
check("a failed read is retried quietly; only giving up fails the run",
      codes == [0] * (fu.GIVE_UP - 1) + [1], codes)
state = fu.load()
check("a conversation that never reads cleanly is given up on",
      state["read"][str(A)]["size"] == A.stat().st_size and "aaaaaaaa-1111" not in state["failed"], state)
fu.ask = fake_extract

# --- dry, apply, undo -------------------------------------------------------------
os.remove(fu.STATE)
B = PROJ / "bbbbbbbb-4444.jsonl"
write(B, [rec("user", "fix the router"), rec("assistant", [{"type": "text", "text": "Reboot it on Sunday."}])])
proposals = {"bbbbbbbb-4444": [{"title": "Reboot the router", "context": ""},
                               {"title": "Label the cables", "context": ""}]}
posted.clear()
fu.dry(["30"])
check("dry files nothing", posted == [] and not os.path.exists(fu.STATE), posted)
plan = json.load(open(fu.PLAN))["conversations"]
check("dry keeps the list it printed", [i["title"] for p in plan for i in p["items"]]
      == ["Reboot the router", "Label the cables"], plan)

OLD = PROJ / "oooooooo-6666.jsonl"   # outside the 30-day window
write(OLD, [rec("user", "ancient"), rec("assistant", [{"type": "text", "text": "ok"}])], age_h=24 * 40)
NOW = PROJ / "nnnnnnnn-7777.jsonl"   # still going when the list was made
write(NOW, [rec("user", "in progress"), rec("assistant", [{"type": "text", "text": "ok"}])], age_h=0.1)
fu.dry(["30"])
check("dry leaves out what is still going", all(p["id"] != "nnnnnnnn-7777" for p in json.load(open(fu.PLAN))["conversations"]))
fu.apply({2})
check("apply files the list minus what was left out",
      [p["title"] for p in posted] == ["Reboot the router"], posted)
check("apply cannot run twice", not os.path.exists(fu.PLAN))
state = fu.load()
check("a conversation older than the window counts as read", str(OLD) in state["read"], state["read"].keys())
check("one still going when the list was made does not", str(NOW) not in state["read"], state["read"].keys())

dbdir = pathlib.Path(fu.DATA) / "state"
dbdir.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(dbdir / "brain.db")
db.execute("CREATE TABLE tasks (id TEXT, title TEXT, archived INT, status TEXT, capture_id TEXT)")
ours = state["filed"]["bbbbbbbb-4444"][0]["capture"]
db.executemany("INSERT INTO tasks VALUES (?,?,?,?,?)", [
    ("t_ours", "Reboot the router", 0, "needs_action", ours),
    ("t_yours", "Your own task it was deduped onto", 0, "needs_action", "c_somebodyelse"),
    ("t_done", "Done already", 0, "completed", ours)])
db.commit()
dropped = []
fu._api = lambda path, payload=None: dropped.append(path) or {}
fu.undo(["all"])
check("undo archives exactly the open tasks it created", dropped == ["/task/t_ours/drop"], dropped)

# --- what you left out stays out ----------------------------------------------------
state = fu.load()
check("what apply left out is remembered",
      [o["title"] for o in state["left_out"].get("bbbbbbbb-4444", [])] == ["Label the cables"],
      state.get("left_out"))
write(B, [rec("user", "the router again"), rec("assistant", [{"type": "text", "text": "Still flaky."}])])
C = PROJ / "cccccccc-8888.jsonl"
write(C, [rec("user", "cables everywhere"), rec("assistant", [{"type": "text", "text": "Label them."}])])
proposals = {"bbbbbbbb-4444": [{"title": "Label the cables", "context": ""},
                               {"title": "Buy a switch", "context": ""}],
             "cccccccc-8888": [{"title": "label the  cables.", "context": ""}]}
posted.clear(); extracted.clear()
fu.run()
check("something left out is not filed by a later read, from any conversation",
      [p["title"] for p in posted] == ["Buy a switch"], posted)
check("and every later read is told about it",
      len(extracted) == 2 and all("- Label the cables" in p for _, p in extracted),
      [p[:300] for _, p in extracted])

# --- ignored on the board ---------------------------------------------------------
write(C, [rec("user", "and the shelf"), rec("assistant", [{"type": "text", "text": "Next week."}])])
proposals = {"cccccccc-8888": [{"title": "Mount the shelf bracket", "context": ""}]}
fu.post = lambda e: posted.append(e) and None
posted.clear()
check("an item you ignored on the board is not an error", fu.run() == 0)
state = fu.load()
check("it counts as left out, not filed",
      "Mount the shelf bracket" in [o["title"] for o in state["left_out"].get("cccccccc-8888", [])]
      and all(f["title"] != "Mount the shelf bracket" for f in state["filed"].get("cccccccc-8888", [])),
      state)
fu.post = fake_post

print()
print("FAILED: " + ", ".join(fails) if fails else "all followups tests passed")
sys.exit(1 if fails else 0)
