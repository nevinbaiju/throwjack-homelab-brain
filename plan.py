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


OPS = {"set_lane", "set_blocked", "clear_blocked", "retitle", "note_append"}


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

    muts: list[dict] = []
    for m_ in (data.get("mutations") or [])[:20]:
        if not isinstance(m_, dict):
            rejected.append("mutation was not an object"); continue
        op = str(m_.get("op", ""))
        tid = str(m_.get("task_id", ""))
        row = rows.get(tid)
        if op not in OPS:
            rejected.append(f"unknown op {op!r}"); continue
        if row is None:
            rejected.append(f"mutation on unknown task {tid!r}"); continue
        if _is_chore(row):
            rejected.append(f"refused mutation on chore {tid}"); continue
        if op == "set_lane":
            lane = str(m_.get("lane", ""))
            # today is reached by being in "today", never by a lane mutation --
            # otherwise the model can bypass the cap.
            if lane not in ("backlog", "in_progress"):
                rejected.append(f"refused set_lane to {lane!r}"); continue
            muts.append({"op": op, "task_id": tid, "lane": lane})
        elif op == "set_blocked":
            who = str(m_.get("blocked_on", "")).strip()[:120]
            if not who:
                rejected.append(f"set_blocked on {tid} with no blocked_on"); continue
            muts.append({"op": op, "task_id": tid, "blocked_on": who})
        elif op == "clear_blocked":
            muts.append({"op": op, "task_id": tid})
        elif op == "retitle":
            title = str(m_.get("title", "")).strip()[:200]
            if not title:
                rejected.append(f"retitle on {tid} with empty title"); continue
            muts.append({"op": op, "task_id": tid, "title": title,
                         "was": row["title"]})
        elif op == "note_append":
            txt = str(m_.get("text", "")).strip()[:500]
            if not txt:
                rejected.append(f"note_append on {tid} with no text"); continue
            muts.append({"op": op, "task_id": tid, "text": txt})

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
