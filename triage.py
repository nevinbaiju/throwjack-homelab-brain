"""Triage: raw captures -> tracked tasks -> cards on the board."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import board
import caldav
import llm
import policy
import score as scoring
import store

SYSTEM = """You are the triage stage of a personal task system. You receive raw captures \
(voice dictation, typed fragments, shared links) and return structured items.

Return ONLY a JSON object: {"items": [...]}. No prose, no markdown fence.

One item per capture, same order, echoing capture_id exactly. Fields:

  capture_id   string, copied exactly from the input
  track        one of: meta, focus, flow, waiting, upkeep, someday
  title        the actionable form (see rules)
  project      short slug if one is evident, else null
  consequence  one of: breaks, costs, improves, optional — or null for meta
  estimate_min integer minutes, your best guess
  due          YYYY-MM-DD if a date is stated or clearly implied, else null
  blocked_on   name of the person being waited on, else null

Track rules:
  meta     work. Marked by source "shortcut-work", a leading "w:", or text that
           reads as private work shorthand you cannot parse. DO NOT interpret,
           expand or rewrite meta titles — copy the text verbatim.
  focus    personal deep work needing loaded context: side projects, coursework.
  flow     personal miscellany, errands, one-off home jobs, messages owed.
  waiting  the user has handed something off and is blocked on another person.
  upkeep   recurring home essentials: cooking, dishes, laundry, trash, groceries.
  someday  ideas, articles, "would be cool if". No deadline, no urgency.

Title rules (all tracks except meta):
  Rewrite into ONE physical next action starting with a verb, doable in a single
  sitting. "Sort out the thesis" is not a task; "Email Dr. Rao about the
  extension" is. If the capture describes a project rather than an action, title
  it with the first concrete step.

Consequence means what happens if this is never done:
  breaks    a person is blocked, a system fails, or a deadline passes with real cost
  costs     no breakage but real cost: money, rework, a worse outcome
  improves  genuinely better if done, nothing bad if not
  optional  would be nice
"""


# P0..P3 at the head of a capture. Parsed here rather than asked of the model:
# the pattern is rigid, and a free-tier model that silently drops it produces a
# task that looks fine and is ranked wrong. Stripping it also removes one piece
# of scaffolding from the title before triage ever sees the text.
_PRIORITY_RE = re.compile(r"^\s*p\s*([0-3])\b[\s:.,;\-\u2013\u2014]*", re.IGNORECASE)

# P0 is not a consequence, it is an override -- see store.PINNED_SCORE.
_PRIORITY_CONSEQUENCE = {1: "breaks", 2: "costs", 3: "improves"}


# Leading scaffolding, stripped in Python. The prompt asks the model to do this
# too, but a free-tier model ignored the instruction on the very first try and
# returned the whole sentence -- and an unreliable rule that silently no-ops is
# worse than no rule. Anchored at the start and applied repeatedly, so only a
# prefix is ever removed: nothing in the middle of a sentence is touched.
_SCAFFOLD_RES = [re.compile(p, re.IGNORECASE) for p in (
    r"^(?:this\s+is\s+)?(?:my\s+)?work(?:\s+at\s+meta)?\s*[.,;:\-]+\s*",
    r"^(?:for\s+)?work\s*[:\-]\s*",
    r"^w\s*[:\-]\s*",
    r"^(?:i\s+)?(?:need|have|want)\s+to\s+",
    r"^(?:i\s+)?should\s+",
    r"^(?:please\s+)?remember\s+to\s+",
    r"^todo\s*[:\-]?\s*",
)]


# The routing phrase IS the meta signal. Stripping it in Python removed the
# only evidence the model had, and a P0 work item came back as track "flow".
# Detect it before removing it, and force the track rather than hoping.
_WORK_SIGNAL = re.compile(
    r"^\s*(?:this\s+is\s+)?(?:my\s+)?work(?:\s+at\s+meta)?\b|^\s*(?:for\s+)?work\s*[:\-]|^\s*w\s*[:\-]",
    re.IGNORECASE)


def looks_like_work(text: str) -> bool:
    """True if the capture opened with a work/meta routing marker."""
    return bool(_WORK_SIGNAL.match(text or ""))


def strip_scaffolding(text: str) -> str:
    """Remove leading routing phrases and filler. Never substitutes a word."""
    out = (text or "").strip()
    changed = True
    while changed and out:
        changed = False
        for rx in _SCAFFOLD_RES:
            new = rx.sub("", out, count=1)
            if new != out:
                out, changed = new.strip(), True
    return out or (text or "").strip()


def split_priority(text: str) -> tuple[int | None, str]:
    """Pull a leading P0..P3 off a capture. Returns (level, remaining text)."""
    m = _PRIORITY_RE.match(text or "")
    if not m:
        return None, (text or "").strip()
    return int(m.group(1)), (text or "")[m.end():].strip()


def ingest_inbox(conn) -> int:
    """Read every inbox file into the DB. Idempotent — ids are the key."""
    inbox = store.BRAIN_ROOT / "inbox"
    added = 0
    for path in sorted(inbox.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("id") and rec.get("text"):
                added += int(store.ingest_capture(conn, rec))
    return added


def _user_message(captures) -> str:
    lines = ["Today is " + datetime.now().strftime("%Y-%m-%d (%A)") + ".", "", "Captures:"]
    for c in captures:
        lines.append(f'- capture_id={c["id"]} source={c["source"]} text="""{c["text"]}"""')
    return "\n".join(lines)


def project_to_board(conn, task_id: str) -> None:
    """Write a task to its lane on CalDAV.

    One task, one reminder, individually checkable. No rollup card: Backlog is
    a Reminders list now, so a project's steps are just items in it.
    """
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return
    cfg = policy.load_config()
    board.write_task(conn, row, alarm_at=board.intended_alarm(row, cfg))


def reconcile(conn) -> dict:
    """Make the database agree with the phone. See board.reconcile."""
    return board.reconcile(conn)


def rescore(conn, cfg: dict | None = None) -> int:
    cfg = cfg or policy.load_config()
    tolerances = cfg.get("tolerance_days", {})
    now = datetime.now()
    for row in store.active_tasks(conn):
        pin = row["pinned_until"]
        if pin:
            try:
                if datetime.fromisoformat(pin) > now:
                    store.set_score(conn, row["id"], scoring.PINNED_SCORE)
                    continue
            except (TypeError, ValueError):
                pass
        value = scoring.score(
            track=row["track"], consequence=row["consequence"], due=row["due"],
            age_days=store.age_days(row, now), tolerance_days=tolerances.get(row["track"]),
            estimate_min=row["estimate_min"], blocked_on=row["blocked_on"],
            blocking_someone=bool(row["blocked_on"]), now=now,
        )
        store.set_score(conn, row["id"], value)
    return len(store.active_tasks(conn))


def sync_rollups(conn, cfg: dict | None = None) -> dict:
    """Retired. Backlog is a real list now, so each step is its own item."""
    return {"rollups": 0}


def reorder_lanes(conn) -> dict[str, int]:
    """Retired. CalDAV has no ordering API — PRIORITY does the sorting, and
    Reminders honours it. Kept as a no-op so callers need not change."""
    return {}


def _promote(conn) -> dict:
    """Refill In Progress. Local import: project imports triage, not the reverse."""
    import project
    try:
        return project.promote(conn)
    except Exception as e:
        print(f"[triage] promote failed: {type(e).__name__}: {e}", flush=True)
        return {"promoted": 0, "held": 0, "error": str(e)}


def rescore_and_sync(conn, summary: dict | None = None) -> dict:
    """Rescore every active task, then push the ones whose PRIORITY bucket moved.

    Order matters. board._priority() reads row["score"], so a card written
    before rescore() lands at PRIORITY 9 regardless of what the task is worth --
    which silently disabled the entire scoring model on the phone. Writing
    after rescore fixes new tasks; re-pushing on a bucket change fixes existing
    ones, whose urgency climbs as a deadline nears.

    Only bucket changes are pushed, not every score change: a score drifting
    from 130.0 to 131.2 renders identically in Reminders, and rewriting the
    card would burn a CalDAV round trip and wake every synced device.
    """
    out = {"repriced": 0, "carded": 0}
    before = {r["id"]: board._priority(r["score"] or 0.0)
              for r in store.active_tasks(conn)}
    rescore(conn)
    for row in store.active_tasks(conn):
        tid = row["id"]
        now_bucket = board._priority(row["score"] or 0.0)
        was = before.get(tid)
        if was == now_bucket:
            continue
        try:
            project_to_board(conn, tid)
            out["carded" if was is None else "repriced"] += 1
        except caldav.CalDavError as e:
            print(f"[triage] board push failed for {tid}: {e}", flush=True)
    if summary is not None:
        summary["carded"] = summary.get("carded", 0) + out["carded"]
        summary["repriced"] = summary.get("repriced", 0) + out["repriced"]
    return out


def run(conn, limit: int = 25) -> dict:
    run_id = store.start_run(conn, "triage")
    summary = {"reconciled": {}, "from_siri": 0, "ingested": 0, "triaged": 0,
               "duplicates": 0, "failed": 0, "carded": 0, "repriced": 0,
               "promoted": {}}
    try:
        board.bootstrap()
        summary["reconciled"] = reconcile(conn)
        summary["from_siri"] = len(board.drain_inbox(conn))
        summary["ingested"] = ingest_inbox(conn)
        captures = [dict(r) for r in store.pending_captures(conn, limit)]
        # Strip P0..P3 before the model sees the text, and remember the level.
        levels: dict[str, int] = {}
        work: set[str] = set()
        for c in captures:
            lvl, stripped = split_priority(c["text"])
            if lvl is not None:
                levels[c["id"]] = lvl
            if looks_like_work(stripped):
                work.add(c["id"])
            c["text"] = strip_scaffolding(stripped) or c["text"]
        if not captures:
            rescore_and_sync(conn, summary)
            import escalate
            summary["escalated"] = escalate.run(conn)
            summary["promoted"] = _promote(conn)
            store.finish_run(conn, run_id, True, json.dumps(summary))
            return summary

        raw, model, ms = llm.complete("brain-fast", SYSTEM, _user_message(captures))
        try:
            items = llm.validate_triage(raw, captures)
            store.log_llm(conn, "brain-fast", model, ms, True, len(items))
        except llm.LLMError as e:
            store.log_llm(conn, "brain-fast", model, ms, False, error=str(e))
            for c in captures:
                store.bump_attempt(conn, c["id"], str(e))
            summary["failed"] = len(captures)
            store.finish_run(conn, run_id, False, str(e))
            return summary

        for item in items:
            existing = store.find_duplicate(conn, item["title"], item["track"])
            if existing:
                store.mark_capture(conn, item["capture_id"], "done", existing)
                summary["duplicates"] += 1
                continue
            task_id = store.create_task(conn, **item)
            store.mark_capture(conn, item["capture_id"], "done", task_id)

            # What you typed wins over whatever the model inferred.
            if item["capture_id"] in work:
                conn.execute("UPDATE tasks SET track='meta' WHERE id=?", (task_id,))
            lvl = levels.get(item["capture_id"])
            if lvl == 0:
                pin = (datetime.now() + timedelta(days=365)).isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE tasks SET consequence='breaks', pinned_until=? WHERE id=?",
                    (pin, task_id))
                summary["pinned"] = summary.get("pinned", 0) + 1
            elif lvl is not None:
                conn.execute("UPDATE tasks SET consequence=? WHERE id=?",
                             (_PRIORITY_CONSEQUENCE[lvl], task_id))

            if item.get("project"):
                import project
                project.log_event(
                    item["project"],
                    f"**captured** `{task_id}`"
                    f"{f' (P{lvl})' if lvl is not None else ''} — {item['title']}")
            # Deliberately NOT written to the board here -- see rescore_and_sync.
            summary["triaged"] += 1

        # Captures the model silently dropped must not vanish.
        handled = {i["capture_id"] for i in items}
        for c in captures:
            if c["id"] not in handled:
                store.bump_attempt(conn, c["id"], "model omitted this capture")
                summary["failed"] += 1

        rescore_and_sync(conn, summary)
        import escalate
        summary["escalated"] = escalate.run(conn)
        summary["promoted"] = _promote(conn)
        sync_rollups(conn)
        reorder_lanes(conn)
        store.finish_run(conn, run_id, True, json.dumps(summary))
        return summary
    except Exception as e:
        store.finish_run(conn, run_id, False, f"{type(e).__name__}: {e}")
        raise
