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
  "open_questions": ["things the user must decide that you cannot"]
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

If existing tasks are listed, do not duplicate them. Only return NEW tasks.
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
        lines.append("Tasks that ALREADY exist — do not repeat these:")
        for row in existing:
            lines.append(f'  - [{row["status"]}] {row["title"]}')
        lines.append("")
    lines += ["The dump:", '"""', dump.strip(), '"""']
    return "\n".join(lines)


def validate_plan(raw: str) -> dict:
    data = llm._loads(raw)
    proj = data.get("project") or {}
    tasks_in = data.get("tasks")
    if not isinstance(tasks_in, list) or not tasks_in:
        raise llm.LLMError("plan has no tasks")

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

    if not tasks:
        raise llm.LLMError("no usable tasks in plan")

    questions = [str(q).strip()[:300] for q in (data.get("open_questions") or [])
                 if str(q).strip()][:10]
    deadline = proj.get("deadline")
    if not (isinstance(deadline, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline)):
        deadline = None

    return {"name": str(proj.get("name") or "").strip()[:120],
            "summary": str(proj.get("summary") or "").strip()[:1000],
            "deadline": deadline, "tasks": sorted(tasks, key=lambda t: t["seq"]),
            "open_questions": questions}


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

        triage.rescore(conn)
        write_context(conn, slug)
        store.finish_run(conn, run_id, True, json.dumps(summary))
        return summary
    except Exception as e:
        store.finish_run(conn, run_id, False, f"{type(e).__name__}: {e}")
        raise


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

## To close or move a task you worked on

Write a capture whose FIRST line is a directive, using the id from tasks.json:

    done: t_abc123def456

`done` closes it, `doing` promotes it to In Progress, `blocked` moves it to
Waiting, `drop` archives it. Anything after the first line is kept as a note.
Without this, nothing you do can close a card — captures only ever create.

## When something new becomes actionable

Write a NEW markdown file into `captures/`, one item per file:

    captures/{datetime.now():%Y-%m-%d}-short-slug.md

Plain prose is enough — one sentence. These are picked up when Nevin runs a
sync, turned into items on his task lists, and the file is moved to
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

    slugs = [r["project"] for r in conn.execute(
        "SELECT DISTINCT project FROM tasks "
        "WHERE archived=0 AND status!='completed' AND project IS NOT NULL AND project!=''")]

    for slug in slugs:
        room = FOCUS_CAP - store.in_progress_count(conn, slug)
        if room <= 0:
            continue
        candidates = conn.execute(
            "SELECT * FROM tasks WHERE archived=0 AND status!='completed' "
            "AND project=? AND lane='backlog' ORDER BY seq, score DESC", (slug,)).fetchall()
        for row in candidates:
            if room <= 0:
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
