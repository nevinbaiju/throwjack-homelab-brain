"""Projects: turn an idea dump into a sequenced plan, and keep a context
folder your other LLMs can read.

Triage maps one capture to one task. A dump is the other shape: one blob of
thinking that has to become a project plus an ordered set of next actions,
without losing anything you said.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import board
import caldav
import llm
import policy
import store
import triage

CONTEXTS = store.BRAIN_ROOT / "contexts"
FOCUS_CAP = 3

SYSTEM = """You turn a raw brain-dump about one project into a concrete plan.

Return ONLY a JSON object, no prose, no markdown fence:

{
  "project":  {"name": "...", "summary": "...", "deadline": "YYYY-MM-DD or null"},
  "tasks":    [{"title": "...", "why": "...", "consequence": "breaks|costs|improves|optional",
                "estimate_min": 45, "due": "YYYY-MM-DD or null", "seq": 1,
                "start_now": true}],
  "open_questions": ["things the user must decide that you cannot"],
  "updates":  [{"id": "t_abc123def456", "title": "...", "why": "...", "seq": 2,
                "consequence": "...", "estimate_min": 30, "due": "...", "drop": false}]
}

Rules for tasks:
- Each title is ONE physical next action, starting with a verb, doable in a
  single sitting. Not "prepare for system design" but "Write out the DoorDash
  delivery-matching data model on paper".
- "why" is one short clause explaining what it unblocks. The user has ADHD;
  a task whose point is not obvious will not get started.
- "seq" orders the work: 1 first. Things that unblock other things come first.
- "start_now" is true for AT MOST 3 tasks — the ones to do first. Everything
  else is false. Do not exceed 3 under any circumstances.
- estimate_min must be realistic for one sitting: prefer 25-90 minutes. If
  something needs longer, split it into several tasks.
- Cover everything in the dump. If the user mentioned a worry, a gap or a
  deadline, it must appear as a task or an open question. Losing something the
  user said is the worst failure here.
- Do NOT invent scope the user did not mention.

Existing tasks are listed with their ids. Do not duplicate them as new tasks.

"updates" REFRAMES work that already exists. Use it when newer context shows
an existing task is vague, misordered, wrongly scoped or no longer needed.
This is the point of the field: the plan was often written from a thinner
dump than you are holding now, and a task the user cannot start is worse
than no task.

- Include "id" plus ONLY the fields you are changing. Omit the rest.
- Retitle when the title is not a concrete physical next action.
- Reorder with "seq" when the sequence no longer reflects what unblocks what.
- "drop": true archives a task the newer context has made irrelevant. Use it
  sparingly and never for something merely inconvenient.
- Leave a task alone if you would only be rewording it. Churn costs the user
  a re-read of a card they already recognise.
- An id you were not given does not exist. Never invent one.
"""


def is_project_dir(path) -> bool:
    """A real project folder. Excludes dotted directories — .stfolder is
    Syncthing's marker, and writing a project into it both pollutes the marker
    and invents a phantom project named `.stfolder`."""
    return path.is_dir() and not path.name.startswith(".")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9-]", "", re.sub(r"[\s_]+", "-", (text or "").lower()))[:40] or "project"


def _user_message(slug: str, dump: str, existing) -> str:
    lines = [f"Today is {datetime.now().strftime('%Y-%m-%d (%A)')}.",
             f"Project slug: {slug}", ""]
    if existing:
        lines.append("Tasks that ALREADY exist. Do not repeat them as new; "
                     "refactor them through \"updates\" if newer context warrants it:")
        for row in existing:
            seq = row["seq"] if "seq" in row.keys() else ""
            lines.append(f'  - {row["id"]} [{row["status"]}] (seq {seq}) {row["title"]}')
        lines.append("")
    lines += ["The dump:", '"""', dump.strip(), '"""']
    return "\n".join(lines)


def validate_plan(raw: str) -> dict:
    data = llm._loads(raw)
    proj = data.get("project") or {}
    tasks_in = data.get("tasks")
    if not isinstance(tasks_in, list):
        tasks_in = []

    tasks, started = [], 0
    for n, t in enumerate(tasks_in):
        if not isinstance(t, dict):
            continue
        title = (t.get("title") or "").strip()
        if not title:
            continue

        cons = str(t.get("consequence", "")).lower().strip()
        if cons not in llm.CONSEQUENCES:
            cons = "improves"

        est = llm.coerce_minutes(t.get("estimate_min"))

        due = t.get("due")
        if not (isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due)):
            due = None

        seq = t.get("seq")
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else n + 1

        # The cap is enforced here, not trusted to the model.
        start = bool(t.get("start_now")) and started < FOCUS_CAP
        started += int(start)

        tasks.append({"title": title[:255], "why": (t.get("why") or "").strip()[:300],
                      "consequence": cons, "estimate_min": est, "due": due,
                      "seq": seq, "start_now": start})

    # Emptiness is checked after updates are parsed: a dump whose whole point
    # is reframing existing work is valid and returns no new tasks at all.

    questions = [str(q).strip()[:300] for q in (data.get("open_questions") or [])
                 if str(q).strip()][:10]
    deadline = proj.get("deadline")
    if not (isinstance(deadline, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline)):
        deadline = None

    # Updates carry only the fields the model chose to change, so each is
    # validated on its own and absent keys stay absent -- writing a default
    # here would silently overwrite good data with a guess.
    updates = []
    for u in (data.get("updates") or []):
        if not isinstance(u, dict):
            continue
        tid = str(u.get("id") or "").strip()
        if not re.fullmatch(r"t_[0-9a-f]{6,}", tid):
            continue
        fields: dict = {}
        if isinstance(u.get("title"), str) and u["title"].strip():
            fields["title"] = u["title"].strip()[:255]
        if isinstance(u.get("why"), str) and u["why"].strip():
            fields["why"] = u["why"].strip()[:300]
        if isinstance(u.get("seq"), int) and not isinstance(u.get("seq"), bool):
            fields["seq"] = u["seq"]
        c = str(u.get("consequence", "")).lower().strip()
        if c in llm.CONSEQUENCES:
            fields["consequence"] = c
        if u.get("estimate_min") is not None:
            fields["estimate_min"] = llm.coerce_minutes(u["estimate_min"])
        d = u.get("due")
        if isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            fields["due"] = d
        if u.get("drop") is True:
            fields = {"drop": True}
        if fields:
            updates.append({"id": tid, **fields})

    if not tasks and not updates:
        raise llm.LLMError("plan has neither tasks nor updates")

    return {"name": str(proj.get("name") or "").strip()[:120],
            "summary": str(proj.get("summary") or "").strip()[:1000],
            "deadline": deadline, "tasks": sorted(tasks, key=lambda t: t["seq"]),
            "open_questions": questions, "updates": updates}


def dump(conn, slug: str, text: str, alias: str = "brain-plan") -> dict:
    """Decompose a brain-dump into tasks on the board."""
    slug = slugify(slug)
    run_id = store.start_run(conn, f"dump:{slug}")
    summary = {"project": slug, "created": 0, "duplicates": 0, "started": 0, "questions": 0}
    try:
        # The dump itself is archived verbatim before anything interprets it.
        raw_dir = CONTEXTS / slug / "dumps"
        raw_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (raw_dir / f"{stamp}.md").write_text(text, encoding="utf-8")

        triage.reconcile(conn)      # respect anything you changed by hand first
        existing = store.project_tasks(conn, slug)
        raw, model, ms = llm.complete(alias, SYSTEM, _user_message(slug, text, existing),
                                      max_tokens=8192)
        try:
            plan = validate_plan(raw)
            store.log_llm(conn, alias, model, ms, True, len(plan["tasks"]))
        except llm.LLMError as e:
            store.log_llm(conn, alias, model, ms, False, error=str(e))
            store.finish_run(conn, run_id, False, str(e))
            raise

        store.upsert_project(conn, slug, plan["name"] or slug, plan["summary"],
                             plan["deadline"], json.dumps(plan["open_questions"]))
        summary["questions"] = len(plan["open_questions"])

        room = FOCUS_CAP - store.in_progress_count(conn, slug)
        for t in plan["tasks"]:
            if store.find_duplicate(conn, t["title"], "focus"):
                summary["duplicates"] += 1
                continue
            lane = "in_progress" if (t["start_now"] and room > 0) else "backlog"
            task_id = store.create_task(
                conn, title=t["title"], note=t["why"], track="focus", project=slug,
                consequence=t["consequence"], estimate_min=t["estimate_min"],
                due=t["due"] or plan["deadline"], lane=lane,
            )
            conn.execute("UPDATE tasks SET seq=?, why=? WHERE id=?", (t["seq"], t["why"], task_id))
            if lane == "in_progress":
                room -= 1
                summary["started"] += 1
            triage.project_to_board(conn, task_id)
            summary["created"] += 1

        summary["refactored"] = apply_updates(conn, slug, plan.get("updates") or [])

        triage.rescore(conn)
        write_context(conn, slug)
        store.finish_run(conn, run_id, True, json.dumps(summary))
        return summary
    except Exception as e:
        store.finish_run(conn, run_id, False, f"{type(e).__name__}: {e}")
        raise


def apply_updates(conn, slug: str, updates: list[dict]) -> dict:
    """Apply the planner's refactors to tasks that already exist.

    Scoped to this project: an id from another project, or one the model
    invented, is ignored rather than guessed at. Only the fields present in an
    update are written, so a partial update cannot blank good data.
    """
    out = {"retitled": 0, "reordered": 0, "rescoped": 0, "dropped": 0, "ignored": 0}
    for u in updates:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND project=? AND archived=0",
            (u["id"], slug)).fetchone()
        if row is None:
            out["ignored"] += 1
            continue

        if u.get("drop"):
            conn.execute("UPDATE tasks SET archived=1, updated_at=? WHERE id=?",
                         (store.now_iso(), u["id"]))
            try:
                board.remove_task(conn, row)
            except caldav.CalDavError as e:
                print(f"[refactor] {u['id']} card not removed: {e}", flush=True)
            out["dropped"] += 1
            log_event(slug, f"**dropped** `{u['id']}` — {row['title']}")
            continue

        sets, vals = [], []
        for col in ("title", "why", "seq", "consequence", "estimate_min", "due"):
            if col in u and u[col] != row[col]:
                sets.append(f"{col}=?")
                vals.append(u[col])
                out["retitled" if col == "title" else
                    "reordered" if col == "seq" else "rescoped"] += 1
        if not sets:
            continue
        vals += [store.now_iso(), u["id"]]
        conn.execute(f"UPDATE tasks SET {', '.join(sets)}, updated_at=? WHERE id=?", vals)
        if "title" in u:
            log_event(slug, f"**retitled** `{u['id']}` — {row['title']!r} -> {u['title']!r}")
        else:
            log_event(slug, f"**rescoped** `{u['id']}` — {row['title']} "
                            f"({', '.join(k for k in u if k != 'id')})")
        try:
            triage.project_to_board(conn, u["id"])
        except caldav.CalDavError as e:
            print(f"[refactor] {u['id']} board update failed: {e}", flush=True)
    return out


def write_context(conn, slug: str) -> Path:
    """Refresh contexts/<slug>/ so another LLM can pick the project up cold."""
    root = CONTEXTS / slug
    (root / "captures").mkdir(parents=True, exist_ok=True)
    proj = store.get_project(conn, slug)
    rows = store.project_tasks(conn, slug)

    brief = root / "BRIEF.md"
    if not brief.exists():                       # yours to edit; never overwritten
        brief.write_text(
            f"# {proj['name'] if proj else slug}\n\n"
            "_Your notes. The brain never overwrites this file._\n\n"
            "## What this is\n\n## Constraints\n\n## Deadlines\n", encoding="utf-8")

    questions = json.loads(proj["questions"]) if proj else []
    done = [r for r in rows if r["status"] == "completed"]
    doing = [r for r in rows if r["lane"] == "in_progress" and r["status"] != "completed"]
    todo = [r for r in rows if r["lane"] == "backlog" and r["status"] != "completed"]

    lines = [f"# {proj['name'] if proj else slug} — state",
             f"_Generated by the brain at {store.now_iso()}. Do not edit; your notes go in BRIEF.md._", ""]
    if proj and proj["summary"]:
        lines += [proj["summary"], ""]
    if proj and proj["deadline"]:
        lines += [f"**Deadline:** {proj['deadline']}", ""]
    lines += [f"**Progress:** {len(done)} done / {len(rows)} total", ""]
    for label, group in (("In progress", doing), ("Next up", todo), ("Done", done)):
        if not group:
            continue
        lines.append(f"## {label}")
        for r in group:
            est = f" _({r['estimate_min']}m)_" if r["estimate_min"] else ""
            why = f" — {r['why']}" if r["why"] else ""
            lines.append(f"- {r['title']}{est}{why}")
        lines.append("")
    if questions:
        lines += ["## Open questions for me", *[f"- {q}" for q in questions], ""]
    lines += ["## How to contribute",
              "Drop a markdown file in `captures/` with anything that became actionable.",
              "The brain ingests and files it. Do not edit STATE.md or tasks.json.", ""]
    (root / "STATE.md").write_text("\n".join(lines), encoding="utf-8")

    # Claude Code reads CLAUDE.md (never AGENTS.md) from the cwd and every
    # parent. @-imports are expanded INTO context at launch, so importing
    # STATE.md means the assistant starts the session already knowing where
    # things stand — no tool call, no "go read this file" instruction.
    (root / "CLAUDE.md").write_text(f"""# {proj['name'] if proj else slug}

@STATE.md
@BRIEF.md

You are working on this project with Nevin. This folder is shared state with
his task system (the Focus Board Brain) and syncs from his home server.

The current state of the work is imported above — do not re-plan anything it
already lists as in progress or done. `tasks.json` has the same data with task
ids and status if you need it; read it only when you need the ids.

## Start by asking him about the project

Do this once, near the start of a session, before doing the work.

The plan above was written by a small free-tier model from whatever he
happened to dump at the time. It has no access to the code, the repo, or
anything he did not think to type. You do. So the task titles are often
vaguer than they should be, the ordering often does not reflect what actually
unblocks what, and some tasks are scoped wrong.

Ask him the questions that would change the plan. Not a questionnaire — two
or three real ones, the things you would need to know to do the work well and
cannot find out yourself. Constraints, what he has already decided, what
"done" looks like, what he is actually worried about.

Then write what you learned as a capture whose FIRST line is `replan:`

    captures/{datetime.now():%Y-%m-%d}-what-i-learned.md
    ---
    replan:
    <everything you now understand that the original plan did not>

The brain picks that up on its own timer and re-plans against the tasks that
already exist: it can retitle a vague task, reorder the sequence, rescope an
estimate, or drop work the new context made irrelevant. Nevin does not have to
run anything. That is the mechanism by which your understanding improves his
board.

Every capture, promotion, retitle and drop is appended to `LOG.md` in this
folder, so you can see what the brain did with what you wrote.

You cannot edit tasks directly, and should not try. Improve the context and
the tasks follow.

## To close or move a task you worked on

Write a capture whose FIRST line is a directive, using the id from tasks.json:

    done: t_abc123def456

`done` closes it, `doing` promotes it to In Progress, `blocked` moves it to
Waiting, `drop` archives it. Anything after the first line is kept as a note.
Without this, nothing you do can close a card — captures only ever create.

## When something new becomes actionable

Write a NEW markdown file into `captures/`, one item per file:

    captures/{datetime.now():%Y-%m-%d}-short-slug.md

Plain prose is enough — one sentence. The brain sweeps these on a timer,
turns them into items on his task lists, and moves the file to
`captures/.ingested/`. Do not batch unrelated items into one file.

Good: "The Nyquist plots in problem set 4 need redoing before Thursday."
Bad:  "TODO: various things" — that becomes a card he cannot act on.

He has ADHD. Write the concrete next physical action, not the topic. A task
whose first step is not obvious does not get started.

## Never

- Edit `STATE.md`, `tasks.json` or `CLAUDE.md`. The brain owns them and
  overwrites them on every update. Your edits will be lost.
- Delete anything in `dumps/`. That is his raw thinking, kept verbatim.
- Assume a capture became a task. Check `tasks.json` next session.
""", encoding="utf-8")

    (root / "tasks.json").write_text(json.dumps({
        "project": slug,
        "generated_at": store.now_iso(),
        "deadline": proj["deadline"] if proj else None,
        "open_questions": questions,
        "tasks": [{"id": r["id"], "title": r["title"], "why": r["why"], "lane": r["lane"],
                   "status": r["status"], "estimate_min": r["estimate_min"],
                   "due": r["due"], "seq": r["seq"]} for r in rows],
    }, indent=2), encoding="utf-8")
    return root


def log_event(slug: str | None, line: str) -> None:
    """Append one line to contexts/<slug>/LOG.md.

    The board shows the current state and nothing about how it got there. When
    a task appears retitled, or in a lane you did not put it in, there was no
    way to tell what did it. Best-effort by design: a logging failure must
    never take down a triage pass.
    """
    if not slug:
        return
    try:
        root = CONTEXTS / slug
        if not root.is_dir():
            return
        path = root / "LOG.md"
        if not path.exists():
            path.write_text("# Trace\n\nWhat the brain did, newest last. "
                            "Written by the brain; safe to read, pointless to edit.\n\n",
                            encoding="utf-8")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"- `{datetime.now():%Y-%m-%d %H:%M}`  {line}\n")
    except OSError as e:
        print(f"[log] {slug}: {e}", flush=True)


def promote(conn, cfg: dict | None = None) -> dict:
    """Refill each project's In Progress from its backlog, and announce the move.

    Nothing promoted a task after creation: FOCUS_CAP was consulted once, at
    plan time, so when In Progress emptied the backlog just sat there. This is
    the refill.

    Gated on policy.can_notify for the task's own track, so nothing moves or
    buzzes during downtime -- waking up to three tasks that silently started
    overnight is worse than waking up to none.

    The alarm is set explicitly rather than via alarm_time_for(), which returns
    None for a task with no due date. The point here is to announce the move,
    which has nothing to do with whether a deadline exists.
    """
    cfg = cfg or policy.load_config()
    now = datetime.now()
    out: dict = {"promoted": 0, "held": 0, "titles": []}

    # Tasks with no project are their own group, not skipped. A P0 filed from
    # the phone has no project, and "always outranks" cannot mean "except when
    # it does not belong to anything".
    groups = [r["project"] for r in conn.execute(
        "SELECT DISTINCT project FROM tasks WHERE archived=0 AND status!='completed'")]

    for slug in groups:
        if slug:
            scope, params = "project=?", (slug,)
        else:
            scope, params = "(project IS NULL OR project='')", ()
        in_progress = conn.execute(
            f"SELECT COUNT(*) c FROM tasks WHERE archived=0 AND status!='completed' "
            f"AND {scope} AND lane='in_progress'", params).fetchone()["c"]
        room = FOCUS_CAP - in_progress
        stamp = now.isoformat(timespec="seconds")
        # Pinned first, then sequence, then score. A P0 is an override: it is
        # promoted even when In Progress is already at the cap, because the
        # whole point of saying P0 is that it displaces the plan.
        candidates = conn.execute(
            f"SELECT * FROM tasks WHERE archived=0 AND status!='completed' "
            f"AND {scope} AND lane='backlog' "
            f"ORDER BY CASE WHEN pinned_until IS NOT NULL AND pinned_until > ? "
            f"THEN 0 ELSE 1 END, seq, score DESC", (*params, stamp)).fetchall()
        for row in candidates:
            pinned = bool(row["pinned_until"]) and str(row["pinned_until"]) > stamp
            if room <= 0 and not pinned:
                break
            if not policy.can_notify(row["track"], now, cfg):
                out["held"] += 1
                continue
            ts = store.now_iso()
            # A promotion is a fresh nudge, not a continuation of an old
            # escalation chain: reset the counter and own last_alarm_at
            # outright, because write_task only sets it when it is NULL.
            #
            # The alarm is a minute out, not "now": escalate_tasks re-arms
            # anything whose last_alarm_at has already passed, and running in
            # the same pass it would push this to +2h before the phone ever
            # saw it.
            announce = now + timedelta(minutes=1)
            conn.execute(
                "UPDATE tasks SET lane='in_progress', touched_at=?, updated_at=?, "
                "escalations=0, last_alarm_at=? WHERE id=?",
                (ts, ts, announce.isoformat(timespec="seconds"), row["id"]))
            fresh = conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            try:
                board.write_task(conn, fresh, alarm_at=announce)
                out["promoted"] += 1
                out["titles"].append(fresh["title"])
                log_event(slug, f"**promoted**{' (P0)' if pinned else ''} "
                                f"`{row['id']}` — {fresh['title']}")
                if not pinned:
                    room -= 1
            except caldav.CalDavError as e:
                # Put it back: a lane change the phone never saw is a lie.
                conn.execute("UPDATE tasks SET lane='backlog' WHERE id=?", (row["id"],))
                print(f"[promote] {row['id']} rolled back: {e}", flush=True)
    return out


def ingest_all_captures_by_project(conn) -> dict[str, int]:
    """Sweep every project's captures/ folder, returning {slug: files_pulled}.

    Only slugs that actually yielded files appear. The triage loop uses that to
    rewrite STATE.md/tasks.json for exactly the projects that moved, instead of
    churning every project's files on every pass -- which would also wake
    Syncthing and every device holding those folders.

    No change detection is needed: ingest_captures() *moves* each file into
    captures/.ingested/, so a file still sitting in captures/ IS the signal.
    Hashing would add state that can go stale for no benefit.
    """
    out: dict[str, int] = {}
    if not CONTEXTS.exists():
        return out
    for folder in sorted(CONTEXTS.iterdir()):
        if is_project_dir(folder):
            n = ingest_captures(conn, folder.name)
            if n:
                out[folder.name] = n
    return out


def ingest_all_captures(conn) -> int:
    """Sweep every project's captures/ folder. Returns the total pulled."""
    return sum(ingest_all_captures_by_project(conn).values())


# An agent can close a task by dropping a capture whose first non-blank line
# is a directive. Completion never goes near the LLM — it is an exact id match.
_DIRECTIVE_RE = re.compile(
    r"^\s*(?:---\s*\n\s*)?(done|doing|blocked|drop)\s*:\s*(t_[0-9a-f]{6,})\b",
    re.IGNORECASE | re.MULTILINE)

# A capture whose first line is "replan:" is enriched CONTEXT, not a task. It
# goes to the planner, which can refactor the tasks that already exist. This is
# what makes dumps/ optional: an agent writes one file and the timer does the
# rest, with no command for Nevin to remember.
_REPLAN_RE = re.compile(r"^\s*(?:replan|context)\s*:[ \t]*", re.IGNORECASE)

_DIRECTIVE_LANE = {"done": ("done", "completed"), "doing": ("in_progress", "needs_action"),
                   "blocked": ("waiting", "needs_action"), "drop": (None, None)}


def apply_directive(conn, text: str) -> str | None:
    """If this capture is a status directive, apply it and return the task id."""
    m = _DIRECTIVE_RE.search(text or "")
    if not m:
        return None
    verb, task_id = m.group(1).lower(), m.group(2)
    row = conn.execute("SELECT * FROM tasks WHERE id=? AND archived=0", (task_id,)).fetchone()
    if row is None:
        return None                      # unknown id: fall through to normal triage

    lane, status = _DIRECTIVE_LANE[verb]
    ts = store.now_iso()
    if verb == "drop":
        conn.execute("UPDATE tasks SET archived=1, updated_at=? WHERE id=?", (ts, task_id))
    else:
        conn.execute(
            "UPDATE tasks SET lane=?, status=?, completed_at=?, touched_at=?, updated_at=? WHERE id=?",
            (lane, status, ts if status == "completed" else None, ts, ts, task_id))

    log_event(row["project"], f"**{verb}** `{task_id}` — {row['title']}")

    # Reflect it on the phone. write_task moves the item to the new lane and
    # sets STATUS itself, keeping the same uid, so there is nothing to clean up.
    fresh = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    try:
        if verb == "drop":
            board.remove_task(conn, fresh)
        else:
            triage.project_to_board(conn, task_id)
    except caldav.CalDavError as e:
        print(f"[directive] {task_id}: board update failed: {e}", flush=True)
    return task_id


def ingest_captures(conn, slug: str) -> int:
    """Pull files another LLM dropped in contexts/<slug>/captures/ into the inbox."""
    folder = CONTEXTS / slug / "captures"
    if not folder.exists():
        return 0
    inbox = store.BRAIN_ROOT / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{datetime.now():%Y-%m-%d}.jsonl"
    archive = CONTEXTS / slug / "captures" / ".ingested"
    archive.mkdir(exist_ok=True)

    count = 0
    for f in sorted(folder.glob("*.md")) + sorted(folder.glob("*.txt")):
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            f.unlink()
            continue
        if _REPLAN_RE.match(text):
            body = _REPLAN_RE.sub("", text, count=1).strip()
            if not body:
                f.unlink()
                continue
            try:
                result = dump(conn, slug, body)
            except Exception as e:
                # Leave the file; the next pass retries. Losing enriched
                # context because one LLM call failed is the worse outcome.
                print(f"[replan] {slug}: {type(e).__name__}: {e}", flush=True)
                continue
            log_event(slug, f"**replanned** from `{f.name}` — "
                            f"{result.get('created', 0)} new, "
                            f"{result.get('refactored', {})}")
            f.rename(archive / f.name)
            count += 1
            continue

        if apply_directive(conn, text) is not None:
            f.rename(archive / f.name)          # handled; never becomes a task
            count += 1
            continue

        rec = {"id": f"c_{store.secrets.token_hex(6)}",
               "at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "local": store.now_iso(), "source": f"project:{slug}",
               "text": text[:4000], "triaged": False}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.rename(archive / f.name)
        count += 1
    return count
