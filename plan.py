"""Today's action items: assemble context, validate a proposal, apply it.

The week of running this on due dates showed the failure: dates you set on
Monday are fiction by Wednesday, and a board full of stale deadlines is one
you stop trusting. This replaces them with a daily pass -- look at what is in
progress, what is in the backlog, what chores are due, and decide what today
actually is.

The model's only job is to PROPOSE that list. Everything it returns goes
through validate() before a single row changes, per the rule in AGENTS.md.
validate() never raises on model output: junk is dropped and reported, because
a planner that 500s on a malformed field is a planner you stop running.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date

import board
import store

# A cap, not a target. The whole point is a list short enough to actually be
# today's list; without a ceiling the model happily proposes fourteen things
# and you are back to a backlog with a different name.
TODAY_CAP = int(os.environ.get("BRAIN_TODAY_CAP", "5"))

# Chores are not tasks (AGENTS.md). iOS owns their recurrence and completion,
# and the brain only ever reads them. They appear in the context so the model
# can see the day is already half full, but no mutation may touch them.
def _is_chore(row) -> bool:
    return str(row["id"]).startswith("u_") or row["track"] == "upkeep"


# Ops a DRAFT may propose. A draft is choosing the day; it has no business
# completing things or inventing tasks.
OPS = {"set_lane", "set_blocked", "clear_blocked", "retitle", "note_append"}

# Ops an UPDATE may additionally propose. Mid-day reality is different: things
# get finished, things appear, things fall off. "add" is here because "the
# boiler guy is coming at 3" is a real thing that happened to your day and
# making you go and file it separately is how a plan stops matching the day.
UPDATE_OPS = OPS | {"complete", "to_today", "from_today", "add"}


def build_context(conn, *, journal: str = "") -> dict:
    """What the model gets to look at. Deliberately small.

    Everything here is already on the board -- this adds no new source of
    truth. Bodies are truncated because a planner does not need the full text
    of a task to decide whether it belongs in today, and a 60k-token prompt
    costs real subscription budget for no better answer.
    """
    def slim(row) -> dict:
        d = {
            "id": row["id"],
            "title": row["title"],
            "lane": row["lane"],
            "track": row["track"],
            "age_days": round(store.age_days(row), 1),
        }
        if row["project"]:
            d["project"] = row["project"]
        if row["blocked_on"]:
            d["blocked_on"] = row["blocked_on"]
        if row["estimate_min"]:
            d["estimate_min"] = row["estimate_min"]
        if row["note"]:
            d["note"] = row["note"][:280]
        return d

    lanes: dict[str, list[dict]] = {}
    for lane in ("today", "in_progress", "backlog"):
        lanes[lane] = [slim(r) for r in store.active_tasks(conn, lane)
                       if not _is_chore(r)]

    chores = [{"id": r["id"], "title": r["title"]}
              for r in store.active_tasks(conn, "upkeep")]

    return {
        "date": date.today().isoformat(),
        "cap": TODAY_CAP,
        "lanes": lanes,
        "chores_today": chores,
        "journal": journal[:2000],
    }


SYSTEM = f"""You plan one person's day. They have ADHD. You are choosing what
goes on today's list and nothing else.

Reply with ONLY a JSON object, no prose, no code fence:

{{"today":    [{{"task_id":"<id>","why":"<max 12 words>"}}],
 "mutations":[{{"op":"set_lane","task_id":"<id>","lane":"backlog|in_progress"}},
              {{"op":"set_blocked","task_id":"<id>","blocked_on":"<who or what>"}},
              {{"op":"clear_blocked","task_id":"<id>"}},
              {{"op":"retitle","task_id":"<id>","title":"<new title>"}},
              {{"op":"note_append","task_id":"<id>","text":"<one line>"}}],
 "notes": "<max 40 words: what you changed and why>"}}

Rules:
- At most {TODAY_CAP} items in "today". Fewer is better than more. A list that
  cannot be finished is worse than a short one.
- Prefer things already in_progress over starting something new. Finishing
  beats starting.
- Anything blocked_on someone else does not belong in today unless the action
  IS chasing them.
- Chores in chores_today already consume the day. Account for them; never
  propose mutations for them.
- "mutations" may be empty. Only propose one when the board is actually wrong
  -- something in_progress that is really blocked, a title that no longer says
  what the next action is.
- Items already in the "today" lane are yesterday's picks that were not
  finished. Decide each one deliberately: keep it if today is still the day,
  otherwise propose set_lane back to backlog or in_progress. Carrying
  something for a third day usually means the task is wrong, not the day --
  say so in notes.
- Never invent a task_id. Use only ids present in the context."""


def validate(conn, raw: str) -> tuple[dict, list[str]]:
    """Model output -> (proposal, rejected reasons). Never raises on junk."""
    rejected: list[str] = []

    text = raw.strip()
    # Models fence JSON despite being asked not to; strip it rather than fail.
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"today": [], "mutations": [], "notes": ""}, ["response was not JSON"]
    if not isinstance(data, dict):
        return {"today": [], "mutations": [], "notes": ""}, ["response was not an object"]

    rows = {r["id"]: r for r in store.active_tasks(conn)}

    today: list[dict] = []
    seen: set[str] = set()
    for item in (data.get("today") or [])[: TODAY_CAP * 3]:
        if not isinstance(item, dict):
            rejected.append("today entry was not an object"); continue
        tid = str(item.get("task_id", ""))
        row = rows.get(tid)
        if row is None:
            rejected.append(f"unknown task_id {tid!r}"); continue
        if _is_chore(row):
            rejected.append(f"{tid} is a chore; iOS owns it"); continue
        if tid in seen:
            continue
        seen.add(tid)
        today.append({"task_id": tid, "title": row["title"],
                      "why": str(item.get("why", ""))[:120]})
    if len(today) > TODAY_CAP:
        rejected.append(f"proposed {len(today)} items; capped at {TODAY_CAP}")
        today = today[:TODAY_CAP]

    # Same validator as the update path -- see _resolve(). OPS (not
    # UPDATE_OPS) is what limits a draft to choosing the day rather than
    # completing or inventing tasks.
    muts: list[dict] = []
    for raw_m in (data.get("mutations") or [])[:20]:
        clean, why = _resolve(conn, rows, raw_m, OPS)
        if clean is None:
            rejected.append(why or "rejected")
        else:
            muts.append(clean)

    return ({"today": today, "mutations": muts,
             "notes": str(data.get("notes", ""))[:400]}, rejected)


def apply(conn, proposal: dict) -> dict:
    """Execute a validated proposal. Only ever called on validate()'s output.

    Nothing is deleted. Tasks leaving today go back to prev_lane, which was
    recorded on the way in -- that is the whole reason the column exists.
    """
    now = store.now_iso()
    picked = {i["task_id"] for i in proposal.get("today", [])}
    moved_in = moved_out = 0

    for row in store.active_tasks(conn, "today"):
        if row["id"] in picked:
            continue
        back = row["prev_lane"] or "backlog"
        conn.execute(
            "UPDATE tasks SET lane=?, prev_lane=NULL, updated_at=?, touched_at=? WHERE id=?",
            (back, now, now, row["id"]))
        moved_out += 1

    for tid in picked:
        row = conn.execute("SELECT lane FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None or row["lane"] == "today":
            continue
        conn.execute(
            "UPDATE tasks SET lane='today', prev_lane=?, updated_at=?, touched_at=? WHERE id=?",
            (row["lane"], now, now, tid))
        moved_in += 1

    applied = 0
    for m_ in proposal.get("mutations", []):
        tid, op = m_["task_id"], m_["op"]
        if op == "set_lane":
            # Never clobber prev_lane for a task sitting in today: it is what
            # sends the task home later.
            conn.execute(
                "UPDATE tasks SET lane=?, updated_at=?, touched_at=? WHERE id=? AND lane!='today'",
                (m_["lane"], now, now, tid))
        elif op == "set_blocked":
            conn.execute("UPDATE tasks SET blocked_on=?, updated_at=? WHERE id=?",
                         (m_["blocked_on"], now, tid))
        elif op == "clear_blocked":
            conn.execute("UPDATE tasks SET blocked_on=NULL, updated_at=? WHERE id=?",
                         (now, tid))
        elif op == "retitle":
            conn.execute("UPDATE tasks SET title=?, updated_at=? WHERE id=?",
                         (m_["title"], now, tid))
        elif op == "note_append":
            conn.execute(
                "UPDATE tasks SET note = CASE WHEN note='' THEN ? ELSE note || char(10) || ? END,"
                " updated_at=? WHERE id=?",
                (m_["text"], m_["text"], now, tid))
        applied += 1

    return {"today": len(picked), "moved_in": moved_in,
            "moved_out": moved_out, "mutations": applied}


# --------------------------------------------------------------- job queue ---
# Claude Code runs on the host (that is where the subscription credential
# lives); the brain runs in a container. Rather than opening a port on the
# host that executes a model on request, the worker reaches IN: it polls for
# jobs and posts results back, so nothing new listens anywhere.

def create_job(conn, kind: str, context: dict, *, message: str = "",
               parent: str | None = None) -> str:
    import uuid
    jid = "pj_" + uuid.uuid4().hex[:12]
    now = store.now_iso()
    conn.execute(
        "INSERT INTO plan_jobs (id,kind,state,context,message,parent,created_at,updated_at)"
        " VALUES (?,?,'pending',?,?,?,?,?)",
        (jid, kind, json.dumps(context), message, parent, now, now))
    return jid


def claim_job(conn):
    """Hand the oldest pending job to the worker, exactly once.

    The UPDATE ... WHERE state='pending' is the claim: if two workers race,
    one of them changes zero rows and gets nothing, rather than both running
    the same plan and billing it twice.
    """
    row = conn.execute(
        "SELECT * FROM plan_jobs WHERE state='pending' ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    cur = conn.execute(
        "UPDATE plan_jobs SET state='running', updated_at=? WHERE id=? AND state='pending'",
        (store.now_iso(), row["id"]))
    return row if cur.rowcount else None


def finish_job(conn, job_id: str, raw: str, proposal: dict, rejected: list) -> None:
    conn.execute(
        "UPDATE plan_jobs SET state='done', raw=?, proposal=?, rejected=?, updated_at=?"
        " WHERE id=?",
        (raw, json.dumps(proposal), json.dumps(rejected), store.now_iso(), job_id))


def fail_job(conn, job_id: str, error: str) -> None:
    conn.execute(
        "UPDATE plan_jobs SET state='failed', error=?, updated_at=? WHERE id=?",
        (error[:2000], store.now_iso(), job_id))


def get_job(conn, job_id: str):
    return conn.execute("SELECT * FROM plan_jobs WHERE id=?", (job_id,)).fetchone()


def revise_context(conn, parent_row, message: str) -> dict:
    """Context for a revision: the board as it is NOW, plus what was proposed
    and what you said about it.

    The board is re-read rather than reused from the parent job, because you
    may have ticked something off on your phone between the draft and the
    argument about it -- and a planner that reasons about a task you already
    finished is one you stop believing.
    """
    ctx = build_context(conn)
    try:
        prev = json.loads(parent_row["proposal"] or "{}")
    except ValueError:
        prev = {}
    ctx["previous_proposal"] = prev
    ctx["user_says"] = message[:2000]
    return ctx


REVISE_SYSTEM = SYSTEM + """

This is a REVISION. "previous_proposal" is what you last suggested and
"user_says" is their reaction to it. They are the authority on their own day:
if they say something is blocked, wrong, bigger than you thought, or simply
not today, that is a fact, not an opinion to weigh. Re-emit the COMPLETE
proposal, not a diff -- keep the parts they did not object to."""


# ------------------------------------------------------- mid-day updates ---
# Once a plan is fixed, you do not want to re-plan the day because one thing
# finished. You want to say what happened and have only that change. This is
# a separate call with a separate prompt and a separate op set: the update
# path can complete and create, and it never re-emits the day's list, so
# there is no way for "the NDA is done" to quietly reshuffle everything else.

UPDATE_SYSTEM = """You are UPDATING a plan that is already fixed for today.
You are not re-planning. The person is telling you what happened.

Reply with ONLY a JSON object, no prose, no code fence:

{"mutations":[
   {"op":"complete","task_id":"<id>"},
   {"op":"from_today","task_id":"<id>"},
   {"op":"to_today","task_id":"<id>"},
   {"op":"set_blocked","task_id":"<id>","blocked_on":"<who or what>"},
   {"op":"clear_blocked","task_id":"<id>"},
   {"op":"retitle","task_id":"<id>","title":"<new title>"},
   {"op":"note_append","task_id":"<id>","text":"<one line>"},
   {"op":"add","title":"<new task>","lane":"today|backlog","note":"<optional>"}],
 "notes":"<max 40 words: what you changed>"}

Rules:
- Change ONLY what their message implies. Anything they did not mention stays
  exactly as it is. Do not tidy, do not re-prioritise, do not re-order.
- One message often contains several changes. Emit one mutation per change;
  there is no limit and no need to pick the most important one.
- "finished / done / sorted that" -> complete.
- "not happening today / pushing that" -> from_today.
- "need to do X today too" -> to_today if X already exists, otherwise add
  with lane "today".
- "waiting on someone / can't until X" -> set_blocked, and from_today as well
  if it cannot happen today at all.
- Something that is new and NOT for today -> add with lane "backlog".
- Never invent a task_id. Use only ids present in the context. For genuinely
  new things use "add", which needs no id.
- If their message implies no change to the board, return an empty mutations
  list and say why in notes."""


def _resolve(conn, rows, m, allowed) -> tuple[dict | None, str | None]:
    """One mutation -> (clean mutation, rejection reason). Shared by both
    paths, because two copies of this would drift and the drifted one would
    be the half that stopped refusing things."""
    if not isinstance(m, dict):
        return None, "mutation was not an object"
    op = str(m.get("op", ""))
    if op not in allowed:
        return None, f"unknown or disallowed op {op!r}"

    if op == "add":
        title = str(m.get("title", "")).strip()[:200]
        if not title:
            return None, "add with no title"
        lane = str(m.get("lane", "backlog"))
        if lane not in ("today", "backlog", "in_progress"):
            lane = "backlog"
        return {"op": "add", "title": title, "lane": lane,
                "note": str(m.get("note", ""))[:500]}, None

    tid = str(m.get("task_id", ""))
    row = rows.get(tid)
    if row is None:
        return None, f"mutation on unknown task {tid!r}"
    if _is_chore(row):
        return None, f"refused mutation on chore {tid}"

    if op == "set_lane":
        lane = str(m.get("lane", ""))
        if lane not in ("backlog", "in_progress"):
            return None, f"refused set_lane to {lane!r}"
        return {"op": op, "task_id": tid, "lane": lane}, None
    if op == "set_blocked":
        who = str(m.get("blocked_on", "")).strip()[:120]
        if not who:
            return None, f"set_blocked on {tid} with no blocked_on"
        return {"op": op, "task_id": tid, "blocked_on": who}, None
    if op == "retitle":
        title = str(m.get("title", "")).strip()[:200]
        if not title:
            return None, f"retitle on {tid} with empty title"
        return {"op": op, "task_id": tid, "title": title, "was": row["title"]}, None
    if op == "note_append":
        txt = str(m.get("text", "")).strip()[:500]
        if not txt:
            return None, f"note_append on {tid} with no text"
        return {"op": op, "task_id": tid, "text": txt}, None
    if op in ("clear_blocked", "complete", "to_today", "from_today"):
        return {"op": op, "task_id": tid, "title": row["title"]}, None
    return None, f"unhandled op {op!r}"


def validate_update(conn, raw: str) -> tuple[dict, list[str]]:
    """Update output -> (proposal, rejections). Mutations only, never a day."""
    rejected: list[str] = []
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"mutations": [], "notes": ""}, ["response was not JSON"]
    if not isinstance(data, dict):
        return {"mutations": [], "notes": ""}, ["response was not an object"]

    # An update that re-emits the day is the exact failure this path exists to
    # prevent. Drop it loudly rather than silently ignoring it.
    if data.get("today"):
        rejected.append("ignored a 'today' list: an update may not re-plan the day")

    rows = {r["id"]: r for r in store.active_tasks(conn)}
    muts: list[dict] = []
    for raw_m in (data.get("mutations") or [])[:40]:
        clean, why = _resolve(conn, rows, raw_m, UPDATE_OPS)
        if clean is None:
            rejected.append(why or "rejected")
        else:
            muts.append(clean)

    # The cap is advisory here, not enforced. Mid-day, "the boiler guy is
    # coming at 3" is not negotiable just because the list was already full --
    # but you should be told the day is now overcommitted.
    adds = sum(1 for m_ in muts if m_["op"] == "to_today"
               or (m_["op"] == "add" and m_["lane"] == "today"))
    removes = sum(1 for m_ in muts if m_["op"] in ("from_today", "complete"))
    if adds:
        current = len([r for r in store.active_tasks(conn, "today")])
        after = current + adds - removes
        if after > TODAY_CAP:
            rejected.append(f"today would hold {after} items, over the cap of {TODAY_CAP}")

    return {"mutations": muts, "notes": str(data.get("notes", ""))[:400]}, rejected


def apply_update(conn, proposal: dict) -> dict:
    """Execute a validated update. Returns what actually happened, per op,
    so the phone can show it rather than claiming success generically."""
    import project

    now = store.now_iso()
    done: list[str] = []

    for m in proposal.get("mutations", []):
        op = m["op"]
        tid = m.get("task_id")

        if op == "add":
            lane = m["lane"]
            # Straight into the lane asked for. Capture would normally triage
            # this, but the whole point is that you already told us what it is
            # and when it matters; sending it round the loop would put it in
            # the inbox to be classified by a model that knows less than you.
            new_id = store.create_task(
                conn, title=m["title"], note=m["note"], track="focus",
                lane=lane)
            if lane == "today":
                conn.execute("UPDATE tasks SET prev_lane='backlog' WHERE id=?", (new_id,))
            done.append(f"added “{m['title']}” to {lane}")

        elif op == "complete":
            # Reuse the one implementation. A second copy here would be the
            # one that stops logging or stops updating the card.
            if project.set_state(conn, tid, "done"):
                done.append(f"completed “{m.get('title', tid)}”")

        elif op == "to_today":
            row = conn.execute("SELECT lane FROM tasks WHERE id=?", (tid,)).fetchone()
            if row and row["lane"] != "today":
                conn.execute(
                    "UPDATE tasks SET lane='today', prev_lane=?, updated_at=?, touched_at=?"
                    " WHERE id=?", (row["lane"], now, now, tid))
                done.append(f"pulled “{m.get('title', tid)}” into today")

        elif op == "from_today":
            row = conn.execute("SELECT lane, prev_lane FROM tasks WHERE id=?", (tid,)).fetchone()
            if row and row["lane"] == "today":
                conn.execute(
                    "UPDATE tasks SET lane=?, prev_lane=NULL, updated_at=?, touched_at=?"
                    " WHERE id=?", (row["prev_lane"] or "backlog", now, now, tid))
                done.append(f"pushed “{m.get('title', tid)}” off today")

        elif op == "set_lane":
            conn.execute(
                "UPDATE tasks SET lane=?, updated_at=?, touched_at=? WHERE id=? AND lane!='today'",
                (m["lane"], now, now, tid))
            done.append(f"moved “{m.get('title', tid)}” to {m['lane']}")
        elif op == "set_blocked":
            conn.execute("UPDATE tasks SET blocked_on=?, updated_at=? WHERE id=?",
                         (m["blocked_on"], now, tid))
            done.append(f"blocked on {m['blocked_on']}")
        elif op == "clear_blocked":
            conn.execute("UPDATE tasks SET blocked_on=NULL, updated_at=? WHERE id=?", (now, tid))
            done.append("unblocked")
        elif op == "retitle":
            conn.execute("UPDATE tasks SET title=?, updated_at=? WHERE id=?",
                         (m["title"], now, tid))
            done.append(f"retitled to “{m['title']}”")
        elif op == "note_append":
            conn.execute(
                "UPDATE tasks SET note = CASE WHEN note='' THEN ? ELSE note || char(10) || ? END,"
                " updated_at=? WHERE id=?", (m["text"], m["text"], now, tid))
            done.append("added a note")

    return {"applied": len(done), "changes": done}


def update_context(conn, message: str) -> dict:
    """What an update gets to see: today in full, the rest in brief.

    The other lanes are included because "I should do the passport thing
    instead" refers to something that is not on today's list, and an update
    that cannot find it would have to invent a duplicate.
    """
    ctx = build_context(conn)
    ctx["user_says"] = message[:2000]
    return ctx
