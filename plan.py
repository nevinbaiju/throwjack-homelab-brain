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


# Ops a DRAFT may propose. It may create tasks: planning a day routinely
# means naming things that are not on the board yet ("today I'm cooking, doing
# Costco, and pushing the Glacier work"). Refusing that made the planner
# answer "I can't create them, so today stays empty", which is useless.
# It may also complete: the commonest thing to say while planning is that
# yesterday's carried items are actually finished, and refusing that forced
# them BACK onto today -- the opposite of what was meant.
OPS = {"set_lane", "set_blocked", "clear_blocked", "retitle", "note_append",
       "add", "complete", "list_add"}

# Ops an UPDATE may additionally propose. Mid-day reality is different:
# things get finished and things fall off a day already in motion.
UPDATE_OPS = OPS | {"to_today", "from_today"}


def build_context(conn, *, journal: str = "", selected: list[str] | None = None,
                  message: str = "") -> dict:
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

    # Shopping lists live only in CalDAV; they are not rows in tasks. Unless
    # they are named here, "add paneer to the costco list" becomes a backlog
    # task called "Costco list: paneer" instead of two items on the list.
    shops = _shopping_collections()

    ctx = {
        "date": date.today().isoformat(),
        "cap": TODAY_CAP,
        "lanes": lanes,
        "chores_today": chores,
        "shopping_lists": shops,
        "journal": journal[:2000],
    }
    # What he ticked before pressing the button. Not a hint -- a decision.
    if selected:
        ctx["selected_by_user"] = [t for t in selected][:50]
    if message:
        ctx["user_says"] = message[:2000]
    return ctx


SYSTEM = f"""You plan one person's day. They have ADHD. You are choosing what
goes on today's list and nothing else.

Reply with ONLY a JSON object, no prose, no code fence:

{{"today":    [{{"task_id":"<id>","why":"<max 12 words>"}}],
 "mutations":[{{"op":"add","title":"<new task>","lane":"today|backlog",
               "consequence":"breaks|costs|improves|optional","note":"<optional>"}},
              {{"op":"set_lane","task_id":"<id>","lane":"backlog|in_progress"}},
              {{"op":"set_blocked","task_id":"<id>","blocked_on":"<who or what>"}},
              {{"op":"clear_blocked","task_id":"<id>"}},
              {{"op":"retitle","task_id":"<id>","title":"<new title>"}},
              {{"op":"note_append","task_id":"<id>","text":"<one line>"}},
              {{"op":"list_add","list":"<key from shopping_lists>","items":["<item>",...]}},
              {{"op":"complete","task_id":"<id>"}}],
 "notes": "<max 40 words: what you changed and why>"}}

HOW TO READ THE INPUT. Three cases, and you are usually in the third:

1. "selected_by_user" present -> they have pointed at those exact tasks.
   What to DO with them comes from "user_says". Read it first:
     - says they are finished / done / already did them
         -> {{"op":"complete"}} for each. Do NOT put them in "today".
     - says to push them / not today / drop them
         -> {{"op":"set_lane"}} back to backlog or in_progress.
     - says nothing about them, or says to do them
         -> they ARE the day: every one goes in "today", ordered, with a why.
   Either way none of them may be ignored, and do not add others unless asked.
2. "user_says" present -> do what it says. If they name something that is
   not a task yet, CREATE it with {{"op":"add"}}. Never reply that you cannot.
3. Both present -> the text says what to do with the ticked tasks, and may
   also name extra things to include. Honour both. This is the common case:
   "these two are done" or "these two plus a Costco run".
4. Neither present -> nothing has been decided, so this is your call: look at
   the lanes and chores_today and propose a sensible day.

They know their day. Do not argue with it, do not substitute your own
priorities, and never answer that something has no id.

- If they name something that is NOT already a task, CREATE it with
  {{"op":"add", ...}}. Use lane "today" if it is for today. You do not need an
  existing id, and "it has no id" is never a reason to leave the day empty.
- Chores from chores_today CAN be today's items. Put their ids in "today"
  like anything else -- a Sunday that is cooking and a Costco run is a real
  day. You may not mutate them; referencing one is not mutating it.
- Change ONLY what they asked for. If they gave you instructions, do not
  volunteer unrelated tidying of the board. A mutation they did not ask for
  is noise at best.

Otherwise, when they have not told you what the day is:
- At most {TODAY_CAP} items. Fewer is better than more.
- Prefer finishing what is in_progress over starting something new.
- Anything blocked_on someone else does not belong in today unless the action
  IS chasing them.
- Propose a mutation only when the board is actually wrong -- something
  in_progress that is really blocked, a title that no longer names the next
  action.
- Items already in the "today" lane are yesterday's picks that were not
  finished. Decide each deliberately: keep it if today is still the day,
  otherwise set_lane it back. Carrying something a third day usually means
  the task is wrong, not the day -- say so in notes.
- Every "add" needs a "consequence": what happens if it is never done.
  breaks = something fails or a deadline is missed, costs = money, time or
  goodwill, improves = better if done, optional = nice to have. It sets the
  task's priority on his phone; omitting it buries the task at the bottom.
- Groceries and shopping go on a LIST, not on the board. "add paneer to the
  costco list" is {{"op":"list_add","list":"shop-costco","items":["paneer"]}},
  never a task. Use only keys present in "shopping_lists".
- A task that is mostly finished with one piece left is not done. Retitle it
  to name the piece that remains, note_append what is already done, and
  set_lane it wherever it actually belongs -- backlog if it is waiting for a
  weekend or a person.
- Never invent a task_id. For something that does not exist yet, emit ONLY
  {{"op":"add","lane":"today",...}} -- do NOT also put a made-up id in
  "today". The add is what puts it on the day; a second invented id is
  rejected and shows as a warning."""


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
        if tid in seen:
            continue
        seen.add(tid)
        today.append({"task_id": tid, "title": row["title"],
                      "why": str(item.get("why", ""))[:120],
                      # A chore in today is a REFERENCE. iOS owns its
                      # recurrence and completion, so apply() must not move it
                      # out of the Upkeep list -- it just appears on the day.
                      "is_chore": _is_chore(row)})
    # Advisory, never truncating. Silently dropping the sixth thing you said
    # you were doing today is worse than an overfull list: you would not know
    # it had gone. The cap is guidance to the model in the prompt and a
    # warning to you here.
    n_today = len(today) + sum(1 for m_ in (data.get("mutations") or [])
                               if isinstance(m_, dict) and m_.get("op") == "add"
                               and m_.get("lane") == "today")
    if n_today > TODAY_CAP:
        rejected.append(f"today holds {n_today} items, over the cap of {TODAY_CAP}")

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



def _sync_projects(conn, slugs: set[str], events: list[tuple[str, str]]) -> list[str]:
    """Write what happened back into each affected project's folder.

    A task is not only a card on a board; if it belongs to a project it is
    also a line in contexts/<slug>/STATE.md and tasks.json, which is what a
    coding agent reads when it picks the project up cold. Changing the lane
    or the title here without refreshing that folder leaves the agent working
    from a stale plan -- re-planning work already chosen for today, or
    chasing a task you renamed.

    Best-effort, exactly like project.log_event: a context write that fails
    must never lose you the plan you just accepted.
    """
    import project

    # write_context FIRST: it creates contexts/<slug>/ if this is the project's
    # first write, and log_event silently returns when that folder is missing.
    # Logging first meant the very first event for a project was dropped.
    written = []
    for slug in sorted(slugs):
        try:
            project.write_context(conn, slug)
            written.append(slug)
        except Exception as e:                               # noqa: BLE001
            print(f"[plan] write_context({slug}) failed: {e}", flush=True)
    for slug, line in events:
        try:
            project.log_event(slug, line)
        except Exception:                                    # noqa: BLE001
            pass
    return written


def _project_of(conn, task_id: str) -> str | None:
    row = conn.execute("SELECT project FROM tasks WHERE id=?", (task_id,)).fetchone()
    return row["project"] if row and row["project"] else None

def apply(conn, proposal: dict) -> dict:
    """Execute a validated proposal. Only ever called on validate()'s output.

    Nothing is deleted. Tasks leaving today go back to prev_lane, which was
    recorded on the way in -- that is the whole reason the column exists.
    """
    now = store.now_iso()
    # Chores are referenced, never moved: iOS owns the Upkeep list and its
    # recurrence. They show on the day; their lane is not ours to change.
    picked = {i["task_id"] for i in proposal.get("today", [])
              if not i.get("is_chore")}
    moved_in = moved_out = 0
    # Completions run first: if you ticked a carried item to say it is
    # finished, it must leave the day rather than be re-selected into it.
    for m_ in proposal.get("mutations", []):
        if m_["op"] == "complete":
            picked.discard(m_.get("task_id"))
    # A lane move changes which bucket the task occupies in its project's
    # STATE.md, so selection alone is enough to make that folder stale.
    slugs: set[str] = set()
    events: list[tuple[str, str]] = []

    for row in store.active_tasks(conn, "today"):
        if row["id"] in picked:
            continue
        back = row["prev_lane"] or "backlog"
        conn.execute(
            "UPDATE tasks SET lane=?, prev_lane=NULL, updated_at=?, touched_at=? WHERE id=?",
            (back, now, now, row["id"]))
        moved_out += 1
        if row["project"]:
            slugs.add(row["project"])
            events.append((row["project"], f"**off today** `{row['id']}` — {row['title']}"))

    for tid in picked:
        row = conn.execute("SELECT lane, title, project FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None or row["lane"] == "today":
            continue
        conn.execute(
            "UPDATE tasks SET lane='today', prev_lane=?, updated_at=?, touched_at=? WHERE id=?",
            (row["lane"], now, now, tid))
        moved_in += 1
        if row["project"]:
            slugs.add(row["project"])
            events.append((row["project"], f"**today** `{tid}` — {row['title']}"))

    applied = 0
    for m_ in proposal.get("mutations", []):
        op = m_["op"]
        if op == "list_add":
            # Shopping items are plain VTODOs in their own collection, the
            # same thing the web form creates. The brain does not track them
            # as tasks -- iOS owns those lists.
            import caldav, uuid as _uuid
            for item in m_["items"]:
                uid = f"g_{_uuid.uuid4().hex[:12]}"
                try:
                    caldav.put_todo(m_["list"], uid, caldav.build_vtodo(uid, item))
                except Exception as e:                       # noqa: BLE001
                    print(f"[plan] list_add {m_['list']}/{item}: {e}", flush=True)
            applied += 1
            continue
        if op == "add":
            new_id = store.create_task(conn, title=m_["title"], note=m_.get("note", ""),
                                       track="focus", lane=m_["lane"],
                                       consequence=m_.get("consequence", "costs"))
            if m_["lane"] == "today":
                conn.execute("UPDATE tasks SET prev_lane='backlog' WHERE id=?", (new_id,))
            applied += 1
            continue
        tid = m_["task_id"]
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
        elif op == "complete":
            # project.set_state owns completion -- it logs, moves the card and
            # sets completed_at. A second copy here would be the one that
            # drifts. It also means a completed task is no longer in `picked`,
            # so it does not get dragged back onto today below.
            import project
            project.set_state(conn, tid, "done")
            picked.discard(tid)
        applied += 1
        if op != "add":
            slug = _project_of(conn, tid)
            if slug:
                slugs.add(slug)
                events.append((slug, f"**{op}** `{tid}`"))

    synced = _sync_projects(conn, slugs, events)
    return {"today": len(picked), "moved_in": moved_in,
            "moved_out": moved_out, "mutations": applied, "projects": synced}


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


def _shopping_collections() -> dict[str, str]:
    """Live shopping collections, discovered rather than hardcoded."""
    try:
        import caldav
        return {c["collection"]: c.get("label") or c["collection"]
                for c in caldav.list_collections()
                if c["collection"].startswith("shop-")}
    except Exception:                                        # noqa: BLE001
        return {}


def _resolve(conn, rows, m, allowed) -> tuple[dict | None, str | None]:
    """One mutation -> (clean mutation, rejection reason). Shared by both
    paths, because two copies of this would drift and the drifted one would
    be the half that stopped refusing things."""
    if not isinstance(m, dict):
        return None, "mutation was not an object"
    op = str(m.get("op", ""))
    if op not in allowed:
        return None, f"unknown or disallowed op {op!r}"

    if op == "list_add":
        coll = str(m.get("list", ""))
        shops = _shopping_collections()
        if coll not in shops:
            return None, f"no shopping list {coll!r} (have: {', '.join(sorted(shops)) or 'none'})"
        items = [str(i).strip()[:255] for i in (m.get("items") or []) if str(i).strip()]
        if not items:
            return None, f"list_add to {coll} with no items"
        return {"op": "list_add", "list": coll, "label": shops[coll],
                "items": items[:40]}, None

    if op == "add":
        title = str(m.get("title", "")).strip()[:200]
        if not title:
            return None, "add with no title"
        lane = str(m.get("lane", "backlog"))
        if lane not in ("today", "backlog", "in_progress"):
            lane = "backlog"
        # Without a consequence the task scores 0, which is PRIORITY 9 on the
        # phone -- the bottom of every list. Tasks that arrive through capture
        # get this classified by the triage model; ones created here bypass
        # that, so the planner supplies it and anything unrecognised lands
        # mid-scale rather than at the floor.
        cq = str(m.get("consequence", "")).strip().lower()
        if cq not in ("breaks", "costs", "improves", "optional"):
            cq = "costs"
        return {"op": "add", "title": title, "lane": lane, "consequence": cq,
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
    slugs: set[str] = set()
    events: list[tuple[str, str]] = []

    for m in proposal.get("mutations", []):
        op = m["op"]
        tid = m.get("task_id")
        if tid:
            slug = _project_of(conn, tid)
            if slug:
                slugs.add(slug)
                events.append((slug, f"**{op}** `{tid}` — {m.get('title', '')}"))

        if op == "list_add":
            # Shopping items are plain VTODOs in their own collection, the
            # same thing the web form creates. The brain does not track them
            # as tasks -- iOS owns those lists.
            import caldav, uuid as _uuid
            for item in m["items"]:
                uid = f"g_{_uuid.uuid4().hex[:12]}"
                try:
                    caldav.put_todo(m["list"], uid, caldav.build_vtodo(uid, item))
                except Exception as e:                       # noqa: BLE001
                    print(f"[plan] list_add {m['list']}/{item}: {e}", flush=True)
            done.append(f"added {len(m['items'])} to {m['label']}")
            continue
        if op == "add":
            lane = m["lane"]
            # Straight into the lane asked for. Capture would normally triage
            # this, but the whole point is that you already told us what it is
            # and when it matters; sending it round the loop would put it in
            # the inbox to be classified by a model that knows less than you.
            new_id = store.create_task(
                conn, title=m["title"], note=m["note"], track="focus",
                lane=lane, consequence=m.get("consequence", "costs"))
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

    # set_state already logs and refreshes for a completion, but every other
    # op here writes raw SQL, so the folder still has to be rebuilt.
    synced = _sync_projects(conn, slugs, events)
    return {"applied": len(done), "changes": done, "projects": synced}


def update_context(conn, message: str) -> dict:
    """What an update gets to see: today in full, the rest in brief.

    The other lanes are included because "I should do the passport thing
    instead" refers to something that is not on today's list, and an update
    that cannot find it would have to invent a duplicate.
    """
    ctx = build_context(conn)
    ctx["user_says"] = message[:2000]
    return ctx
