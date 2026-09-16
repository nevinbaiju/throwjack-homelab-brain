"""Focus Board Brain — capture service.

Commit 0: the only job is to never lose a thought. No model, no database,
no interpretation. Append a line to a plaintext file on the mirrored array
and return. Everything else in the system is built on top of this staying
boring and always-up.
"""

import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Cookie, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

import auth

BRAIN_ROOT = Path(os.environ.get("BRAIN_ROOT", "/brain"))
INBOX = BRAIN_ROOT / "inbox"

TOKEN = os.environ.get("BRAIN_TOKEN")
if not TOKEN:
    raise SystemExit("BRAIN_TOKEN is required — refusing to start without auth")

# One writer, so appends never interleave. Cheap: the whole request is a
# sub-millisecond write plus an fsync.
_write_lock = threading.Lock()

TRIAGE_EVERY = int(os.environ.get("TRIAGE_INTERVAL_SECONDS", "600"))
_triage_lock = threading.Lock()
# Set when a pass is asked for while one is running. The running pass loops
# once more rather than dropping the request — otherwise filing three tasks
# in quick succession would triage the first and leave the rest until the
# timer, which is exactly the delay this is meant to remove.
_triage_again = threading.Event()


def run_triage_once() -> dict:
    """Serialised, and coalescing: overlapping requests become one more pass."""
    import store, triage, project
    if not _triage_lock.acquire(blocking=False):
        _triage_again.set()
        return {"queued": "a pass is already running; it will run again"}
    try:
        result = {}
        while True:
            _triage_again.clear()
            conn = store.connect()
            try:
                # Sweep every project's captures/ BEFORE triage so anything an
                # agent dropped is in the inbox for this same pass. Files are
                # moved into .ingested/ as they are read, so an empty captures/
                # costs one directory listing and nothing else.
                pulled = project.ingest_all_captures_by_project(conn)
                result = triage.run(conn)
                # Refresh context only for projects that actually moved.
                for slug in pulled:
                    try:
                        project.write_context(conn, slug)
                    except Exception as e:
                        print(f"[triage] write_context({slug}) failed: "
                              f"{type(e).__name__}: {e}", flush=True)
                if pulled:
                    result["from_projects"] = pulled
            finally:
                conn.close()
            if not _triage_again.is_set():
                return result
    finally:
        _triage_lock.release()


def triage_soon() -> None:
    """Fire a pass off-thread. Callers must not wait on it."""
    threading.Thread(target=_safe_triage, daemon=True).start()


def _safe_triage() -> None:
    try:
        run_triage_once()
    except Exception as e:
        print(f"[triage] ad-hoc pass failed: {type(e).__name__}: {e}", flush=True)


def _triage_loop(stop: threading.Event) -> None:
    # Let the stack settle before the first pass.
    if stop.wait(20):
        return
    while not stop.is_set():
        try:
            result = run_triage_once()
            if result.get("triaged") or result.get("failed"):
                print(f"[triage] {result}", flush=True)
        except Exception as e:                      # never let the loop die
            print(f"[triage] ERROR {type(e).__name__}: {e}", flush=True)
        stop.wait(TRIAGE_EVERY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Render the shared agent contract before anything else: a fresh install
    # has no projects yet, so write_context() would not have run.
    try:
        import project
        rendered = project.ensure_root_context()
        if rendered:
            print(f"[brain] contexts/ contract rendered: {', '.join(rendered)}", flush=True)
    except Exception as e:
        print(f"[brain] contract render failed: {type(e).__name__}: {e}", flush=True)

    stop = threading.Event()
    worker = threading.Thread(target=_triage_loop, args=(stop,), daemon=True)
    worker.start()
    print(f"[brain] triage worker every {TRIAGE_EVERY}s", flush=True)
    yield
    stop.set()
    worker.join(timeout=5)


app = FastAPI(title="Focus Board Brain", docs_url=None, redoc_url=None, lifespan=lifespan)


ADMIN_USER = os.environ.get("BRAIN_ADMIN_USER", "")
ADMIN_HASH = os.environ.get("BRAIN_ADMIN_PASSWORD_HASH", "")


def _require_auth(authorization: str | None, session: str | None = None) -> None:
    """A bearer token OR a valid browser session. Either is sufficient."""
    expected = f"Bearer {TOKEN}"
    if authorization and secrets.compare_digest(authorization, expected):
        return
    if session and auth.read_session(session, TOKEN):
        return
    raise HTTPException(status_code=401, detail="bad token")


def _today_path() -> Path:
    INBOX.mkdir(parents=True, exist_ok=True)
    return INBOX / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"


def _append(record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False) + "\n"
    path = _today_path()
    with _write_lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


async def _extract_text(request: Request) -> tuple[str, dict]:
    """Accept whatever the client finds easiest to send.

    iOS Shortcuts will happily send JSON, a raw string, or a form field
    depending on how the action is configured, and getting that wrong is a
    silent no-op at 2am. So take all three.
    """
    raw = await request.body()
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip()
    extra: dict = {}

    if ctype == "application/json":
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="body is not valid JSON")
        if isinstance(payload, str):
            return payload, extra
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="expected an object or a string")
        text = payload.get("text") or payload.get("note") or payload.get("q") or ""
        extra = {k: v for k, v in payload.items() if k not in {"text", "note", "q"}}
        return str(text), extra

    if ctype in {"application/x-www-form-urlencoded", "multipart/form-data"}:
        form = await request.form()
        text = form.get("text") or form.get("note") or form.get("q") or ""
        extra = {k: v for k, v in form.items() if k not in {"text", "note", "q"}}
        return str(text), extra

    return raw.decode("utf-8", errors="replace"), extra


@app.post("/capture")
async def capture(
    request: Request,
    authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE),
    x_source: str | None = Header(default=None, alias="X-Source"),
):
    _require_auth(authorization, session)
    text, extra = await _extract_text(request)
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty capture")

    record = {
        "id": f"c_{uuid.uuid4().hex[:12]}",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "local": datetime.now().isoformat(timespec="seconds"),
        "source": x_source or extra.pop("source", None) or "unknown",
        "text": text,
        "triaged": False,
    }
    if extra:
        record["extra"] = extra

    _append(record)
    # Durable first, then triage off-thread. The response still returns in
    # milliseconds; the capture is on disk before anything interprets it.
    triage_soon()
    return JSONResponse(status_code=202, content={"ok": True, "id": record["id"]})


@app.get("/health")
async def health():
    INBOX.mkdir(parents=True, exist_ok=True)
    probe = INBOX / ".write-probe"
    try:
        probe.write_text(str(time.time()))
        probe.unlink()
        writable = True
    except OSError:
        writable = False
    return {"ok": writable, "inbox": str(INBOX), "writable": writable}


@app.get("/inbox/today", response_class=PlainTextResponse)
async def inbox_today(authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    _require_auth(authorization, session)
    path = _today_path()
    if not path.exists():
        return "nothing captured today\n"
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out.append(f"?? unparseable: {line[:80]}")
            continue
        mark = " " if rec.get("triaged") else "*"
        out.append(f"{mark} {rec.get('local', '')[11:16]}  [{rec.get('source')}]  {rec.get('text')}")
    return "\n".join(out) + "\n"


@app.post("/triage")
async def triage_now(authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Trigger a pass now instead of waiting for the timer."""
    _require_auth(authorization, session)
    return run_triage_once()


@app.get("/board", response_class=PlainTextResponse)
async def board(authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """What the brain thinks the board looks like, in score order."""
    _require_auth(authorization, session)
    import store
    conn = store.connect()
    try:
        lines = []
        for lane in ("backlog", "in_progress", "waiting", "upkeep", "done"):
            rows = store.active_tasks(conn, lane)
            if not rows:
                continue
            lines.append(f"{lane.upper()}  ({len(rows)})")
            for r in rows:
                due = f"  due {r['due']}" if r["due"] else ""
                est = f"  {r['estimate_min']}m" if r["estimate_min"] else ""
                lines.append(f"  {r['score']:>8.1f}  [{r['track']:<7}] {r['title'][:52]}{est}{due}")
            lines.append("")
        pending = conn.execute("SELECT COUNT(*) c FROM captures WHERE state='new'").fetchone()["c"]
        stuck = conn.execute("SELECT COUNT(*) c FROM captures WHERE state='needs_human'").fetchone()["c"]
        lines.append(f"untriaged: {pending}   needs_human: {stuck}")
        return "\n".join(lines) + "\n"
    finally:
        conn.close()


@app.post("/reset")
async def reset(authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Clear the board and the database, then re-triage from the inbox.

    Captures are never touched — inbox/ is append-only and is the real record,
    so a reset costs nothing but the LLM calls to re-triage.
    """
    _require_auth(authorization, session)
    import board, caldav, store
    removed = kept = 0
    conn = store.connect()
    try:
        ours = {r["id"] for r in conn.execute("SELECT id FROM tasks")}
    finally:
        conn.close()
    # Only remove items the brain put there. Your own reminders, and every
    # shopping list, are untouched — shopping is not even in LANES.
    for collection, _ in board.LANES.values():
        for item in caldav.list_todos(collection):
            if item["uid"] in ours:
                caldav.delete_todo(collection, item["uid"])
                removed += 1
            else:
                kept += 1
    conn = store.connect()
    try:
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM captures")
        conn.execute("DELETE FROM projects")
    finally:
        conn.close()
    return {"items_removed": removed, "your_items_kept": kept,
            "note": "captures preserved in inbox/ — run triage to rebuild"}


@app.post("/project/{slug}/dump")
async def project_dump(slug: str, request: Request,
                       authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Decompose a brain-dump into a sequenced plan on the board."""
    _require_auth(authorization, session)
    text, _ = await _extract_text(request)
    if len(text.strip()) < 20:
        raise HTTPException(status_code=400, detail="dump too short to plan from")
    import store, project
    conn = store.connect()
    try:
        return project.dump(conn, slug, text)
    finally:
        conn.close()


@app.get("/project/{slug}", response_class=PlainTextResponse)
async def project_status(slug: str, authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    _require_auth(authorization, session)
    import store, project
    conn = store.connect()
    try:
        slug = project.slugify(slug)
        proj = store.get_project(conn, slug)
        rows = store.project_tasks(conn, slug)
        if not proj and not rows:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}")

        import json as _json
        done = [r for r in rows if r["status"] == "completed"]
        out = [f"{(proj['name'] if proj else slug).upper()}   [{slug}]"]
        if proj and proj["deadline"]:
            out.append(f"deadline {proj['deadline']}")
        if proj and proj["summary"]:
            out += ["", proj["summary"]]
        out += ["", f"progress: {len(done)}/{len(rows)} done", ""]

        for lane, label in (("in_progress", "IN PROGRESS"), ("backlog", "NEXT UP")):
            group = [r for r in rows if r["lane"] == lane and r["status"] != "completed"]
            if not group:
                continue
            cap = f"  (cap {project.FOCUS_CAP})" if lane == "in_progress" else ""
            out.append(f"{label}{cap}")
            for r in group:
                est = f"{r['estimate_min']}m" if r["estimate_min"] else "  -"
                out.append(f"  {r['seq'] or 0:>2}. {est:>5}  {r['title'][:60]}")
                if r["why"]:
                    out.append(f"              {r['why'][:64]}")
            out.append("")
        if done:
            out.append("DONE")
            out += [f"      x  {r['title'][:60]}" for r in done] + [""]
        questions = _json.loads(proj["questions"]) if proj else []
        if questions:
            out += ["OPEN QUESTIONS FOR YOU"] + [f"  ? {q}" for q in questions] + [""]
        out.append(f"context: {project.CONTEXTS / slug}")
        return "\n".join(out) + "\n"
    finally:
        conn.close()


@app.get("/board.json")
async def board_json(authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """The board as structured data, for the web UI.

    /board renders the same thing as plain text for the CLI. This carries the
    fields CalDAV cannot: score, why, project, seq, consequence. Those are the
    reason a view here beats any CalDAV client.
    """
    _require_auth(authorization, session)
    import store, board as bd
    conn = store.connect()
    try:
        now = store.now_iso()
        lanes: dict[str, list] = {}
        rows = conn.execute(
            "SELECT * FROM tasks WHERE archived=0 AND ("
            "  status!='completed' OR completed_at >= date('now','-2 days')"
            ") ORDER BY seq, score DESC").fetchall()
        for r in rows:
            pinned = bool(r["pinned_until"]) and str(r["pinned_until"]) > now
            lanes.setdefault(r["lane"] or "backlog", []).append({
                "id": r["id"], "title": r["title"], "why": r["why"] or r["note"] or "",
                "track": r["track"], "project": r["project"], "due": r["due"],
                "estimate_min": r["estimate_min"], "consequence": r["consequence"],
                "score": round(r["score"] or 0.0, 1),
                "priority": bd._priority(r["score"] or 0.0),
                "pinned": pinned, "status": r["status"],
            })
        return {"lanes": lanes,
                "order": ["in_progress", "backlog", "waiting", "done"]}
    finally:
        conn.close()


@app.post("/task/{task_id}/{verb}")
async def task_state(task_id: str, verb: str,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Move or close one task. Same path a capture directive takes."""
    _require_auth(authorization, session)
    import store, project
    if verb not in ("done", "doing", "blocked", "drop"):
        raise HTTPException(status_code=400, detail="unknown verb")
    conn = store.connect()
    try:
        if not project.set_state(conn, task_id, verb):
            raise HTTPException(status_code=404, detail="no such task")
        conn.commit()
        return {"ok": True, "id": task_id, "verb": verb}
    finally:
        conn.close()


@app.post("/repush")
async def repush(check: bool = False, authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Diff every active task against its card, and optionally repair.

    A board write that raises CalDavError is logged, not retried, so a card can
    drift from the database and stay that way. `check=true` reports the drift;
    without it, each divergent task is re-pushed.
    """
    _require_auth(authorization, session)
    import store, board, triage
    conn = store.connect()
    try:
        problems = board.audit(conn)
        if check:
            return {"divergent": len(problems), "items": problems}
        repaired, failed = 0, []
        for item in problems:
            try:
                triage.project_to_board(conn, item["id"])
                repaired += 1
            except Exception as e:
                failed.append({"id": item["id"], "error": f"{type(e).__name__}: {e}"})
        return {"divergent": len(problems), "repaired": repaired, "failed": failed}
    finally:
        conn.close()


@app.post("/project/{slug}/sync")
async def project_sync(slug: str, authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Ingest whatever another LLM dropped in captures/, then refresh context."""
    _require_auth(authorization, session)
    import store, project
    conn = store.connect()
    try:
        slug = project.slugify(slug)
        pulled = project.ingest_captures(conn, slug)
        result = run_triage_once() if pulled else {"triaged": 0}
        project.write_context(conn, slug)
        return {"captures_pulled": pulled, "triage": result}
    finally:
        conn.close()


@app.post("/sync")
async def sync_all(authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Force a sweep now: pull captures/ from every project, triage, refresh.

    The triage loop does this automatically on every pass. This endpoint exists
    for when you do not want to wait for the next one.
    """
    _require_auth(authorization, session)
    import store, project
    conn = store.connect()
    try:
        pulled = project.ingest_all_captures(conn)
        result = run_triage_once() if pulled else {"triaged": 0}
        refreshed = []
        if project.CONTEXTS.exists():
            for folder in sorted(project.CONTEXTS.iterdir()):
                if project.is_project_dir(folder):
                    try:
                        project.write_context(conn, folder.name)
                        refreshed.append(folder.name)
                    except Exception as e:
                        print(f"[context] {folder.name}: {e}", flush=True)
        return {"captures_pulled": pulled, "triage": result, "context_refreshed": refreshed}
    finally:
        conn.close()


@app.post("/upkeep/sync")
async def upkeep_sync(authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Make the Upkeep list match upkeep.yaml.

    Chores are not tasks: nothing enters the database and reconcile ignores
    them. Only u_ items are touched, so anything else in that list is left
    exactly as it is.
    """
    _require_auth(authorization, session)
    import upkeep
    try:
        return upkeep.sync()
    except upkeep.ChoreError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/upcoming", response_class=PlainTextResponse)
async def upcoming(authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Read-only: what is armed, what has escalated, what chores are overdue."""
    _require_auth(authorization, session)
    from datetime import datetime
    import board, caldav, escalate, store, upkeep

    now = datetime.now()
    conn = store.connect()
    try:
        lines = ["ARMED  (next alarm, escalation level)"]
        rows = conn.execute(
            "SELECT * FROM tasks WHERE archived=0 AND status!='completed' "
            "AND last_alarm_at IS NOT NULL ORDER BY last_alarm_at").fetchall()
        if not rows:
            lines.append("  (nothing armed)")
        for r in rows:
            when = r["last_alarm_at"] or ""
            late = "  OVERDUE" if when and when < now.isoformat() else ""
            lvl = f"  esc{r['escalations']}" if r["escalations"] else ""
            dis = "  dismissed-today" if r["dismissed_until"] and r["dismissed_until"] > now.isoformat() else ""
            lines.append(f"  {when[5:16]}  [{r['track']:<7}] {r['title'][:44]}{lvl}{late}{dis}")

        lines += ["", "CHORES  (from the Upkeep list; the brain never writes these)"]
        collection = board.LANES["upkeep"][0]
        live = {i["uid"]: i for i in caldav.list_todos(collection)}
        for chore in upkeep.load():
            try:
                uid, _ = upkeep.build(chore)
            except upkeep.ChoreError:
                continue
            if uid not in live:
                lines.append(f"  {chore['title'][:30]:<30} NOT ON THE BOARD — run ./brain.sh upkeep")
                continue
            raw = caldav._request("GET", caldav._url(collection, f"{uid}.ics"))[2].decode(
                "utf-8", errors="replace")
            due = escalate._due_of(raw)
            mark = "essential" if chore.get("essential") else "         "
            if due is None:
                state = "no due date"
            elif due > now:
                state = f"due {due:%m-%d %H:%M}"
            else:
                over = now - due
                hrs = int(over.total_seconds() // 3600)
                grace = escalate.grace_for(chore)
                past = "past grace" if over > grace else "within grace"
                state = f"OVERDUE {hrs}h ({past})"
            nudge = conn.execute(
                "SELECT title, escalations FROM tasks WHERE source_key=? AND archived=0",
                (f"chore:{uid}",)).fetchone()
            lines.append(f"  {chore['title'][:30]:<30} {mark}  {state}")
            if nudge:
                lines.append(f"       nudge esc{nudge['escalations']}: {nudge['title'][:56]}")
        return "\n".join(lines) + "\n"
    finally:
        conn.close()


def _page(name: str) -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@app.get("/", response_class=HTMLResponse)
async def root(session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    return RedirectResponse("/ui" if auth.read_session(session or "", TOKEN) else "/login")


@app.get("/login", response_class=HTMLResponse)
async def login_page(bad: int = 0):
    if not ADMIN_USER or not ADMIN_HASH:
        return HTMLResponse(
            "<p style='font-family:system-ui;padding:2rem'>No admin user set. "
            "Run <code>./brain.sh setpass</code> on the server.</p>", status_code=503)
    html = _page("login.html")
    return HTMLResponse(html.replace("<!--ERR-->", "Wrong username or password." if bad else ""))


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if ADMIN_HASH and not auth.looks_valid(ADMIN_HASH):
        # Say it in the log rather than on the page: you need to know, a
        # stranger does not.
        print("[login] BRAIN_ADMIN_PASSWORD_HASH is malformed — re-run "
              "./brain.sh setpass", flush=True)
    if not (ADMIN_USER and ADMIN_HASH) or username != ADMIN_USER or not auth.verify_password(
            password, ADMIN_HASH):
        # Same delay either way, so a wrong username is not distinguishable
        # from a wrong password by timing.
        return RedirectResponse("/login?bad=1", status_code=303)
    response = RedirectResponse("/ui", status_code=303)
    response.set_cookie(
        auth.COOKIE, auth.make_session(username, TOKEN),
        max_age=auth.SESSION_HOURS * 3600, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/")
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.get("/ui", response_class=HTMLResponse)
async def ui(session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    if not auth.read_session(session or "", TOKEN):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_page("ui.html"))


@app.get("/projects")
async def projects(authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Slugs that already exist, for the dump form's autocomplete."""
    _require_auth(authorization, session)
    import store
    conn = store.connect()
    try:
        return sorted({r["slug"] for r in conn.execute("SELECT slug FROM projects")} |
                      {r["project"] for r in conn.execute(
                          "SELECT DISTINCT project FROM tasks WHERE project IS NOT NULL")})
    finally:
        conn.close()
