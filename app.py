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


@app.middleware("http")
async def no_store(request: Request, call_next):
    """Never let the browser cache any of this.

    There were no cache headers at all, so the browser was free to apply its
    own heuristics -- which meant a stale ui.html could keep serving old
    JavaScript after a rebuild, and a cached /lists.json could hide a list you
    just created. The page does its own caching in memory, deliberately and
    visibly; the HTTP layer should not be doing it behind our backs.
    """
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


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
        for lane in ("today", "backlog", "in_progress", "waiting", "done"):
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
                "order": ["today", "in_progress", "backlog", "waiting", "done"]}
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


SHOP_PREFIX = "shop-"


def _shop_collections() -> dict[str, str]:
    """Live shopping collections, discovered rather than hardcoded."""
    import caldav
    return {c["collection"]: c["label"] for c in caldav.list_collections()
            if c["collection"].startswith(SHOP_PREFIX)}


def _upkeep_view() -> dict:
    """Every chore with its state, for the Upkeep tab. No CalDAV involved.

    Chores are no longer VTODOs in an Upkeep list, so this reads the YAML and the
    upkeep_state table. Broken chores come back as `errors` rather than raising:
    this file is hand-edited, and a typo must show up as a message on the tab
    instead of a 500 that takes the shopping lists down with it.
    """
    import store, upkeep
    conn = store.connect()
    try:
        chores, errors = upkeep.load_with_errors()
        return {"chores": upkeep.all_states(conn), "errors": errors}
    except Exception as e:                                   # noqa: BLE001
        print(f"[lists] upkeep: {type(e).__name__}: {e}", flush=True)
        return {"chores": [], "errors": [f"{type(e).__name__}: {e}"]}
    finally:
        conn.close()


@app.get("/lists.json")
async def lists_json(authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Upkeep from the brain, shopping lists from CalDAV.

    Upkeep used to be read from a CalDAV collection too. It is now the chore file
    plus upkeep_state, so this endpoint no longer depends on Radicale being up for
    the Upkeep half -- which matters because a CalDAV failure used to 500 the
    whole Lists tab, groceries included.

    Completed shopping items are dropped, not shown struck through: a list you
    have to scroll past yesterday's shopping to read is worse than one that
    just gets shorter.
    """
    _require_auth(authorization, session)
    import caldav, board as bd
    from concurrent.futures import ThreadPoolExecutor

    def live(coll):
        try:
            return [{"uid": x["uid"], "summary": x["summary"],
                     "description": x.get("description") or ""}
                    for x in caldav.list_todos(coll) if x["status"] != "COMPLETED"]
        except caldav.CalDavError as e:
            print(f"[lists] {coll}: {e}", flush=True)
            return []

    shop_map = _shop_collections()
    colls = list(shop_map)

    # Read every collection at once. Serially this was 2.1s for eight of them --
    # each is an independent HTTP round trip to Radicale, and waiting for one
    # before starting the next is the whole cost. They are pure reads, so there
    # is nothing to order.
    fetched = {}
    if colls:
        with ThreadPoolExecutor(max_workers=min(8, len(colls))) as pool:
            fetched = dict(zip(colls, pool.map(live, colls)))

    shops = [{"collection": c, "label": label,
              "items": sorted(fetched.get(c, []), key=lambda i: i["summary"].lower())}
             for c, label in sorted(shop_map.items(), key=lambda kv: kv[1].lower())]
    view = _upkeep_view()
    return {"upkeep": view["chores"], "upkeep_errors": view["errors"],
            "shopping": shops}


@app.post("/lists/add")
async def lists_add(request: Request,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Create a new shopping list. The name becomes its display name."""
    _require_auth(authorization, session)
    import caldav, project
    name, _ = await _extract_text(request)
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="empty name")
    slug = project.slugify(name)
    if not slug:
        raise HTTPException(status_code=400, detail="name has no usable characters")
    coll = SHOP_PREFIX + slug
    caldav.ensure_collection(coll, name[:60])
    return {"ok": True, "collection": coll, "label": name[:60]}


@app.delete("/lists/{collection}")
async def lists_delete(collection: str,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Delete a shopping list and everything in it. Shopping only, no undo."""
    _require_auth(authorization, session)
    import caldav, board as bd
    if not collection.startswith(SHOP_PREFIX) or collection in bd.BRAIN_OWNED:
        raise HTTPException(status_code=400, detail="not a shopping list")
    caldav.delete_collection(collection)
    return {"ok": True, "collection": collection}


@app.post("/list/{collection}/add")
async def list_add(collection: str, request: Request,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Add one item to a shopping list. Shopping only -- upkeep is generated."""
    _require_auth(authorization, session)
    import caldav
    if collection not in _shop_collections():
        raise HTTPException(status_code=400, detail="not a shopping list")
    text, _ = await _extract_text(request)
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty item")
    uid = f"g_{uuid.uuid4().hex[:12]}"
    caldav.put_todo(collection, uid, caldav.build_vtodo(uid, text[:255]))
    return {"ok": True, "uid": uid}


@app.post("/list/{collection}/{uid}/done")
async def list_done(collection: str, uid: str,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Tick a shopping item off.

    Shopping only. Upkeep items carry an RRULE and iOS owns the recurrence --
    completing one from here risks closing the series instead of the occurrence,
    so that tab stays read-only.

    Rewritten from summary + description rather than patched in place, because
    get_todo returns a parsed dict, not the raw ICS. If-Match means a change
    made on the phone since we read it wins instead of being clobbered.
    """
    _require_auth(authorization, session)
    import caldav
    if collection not in _shop_collections():
        raise HTTPException(status_code=400, detail="not a shopping list")
    current = [x for x in caldav.list_todos(collection) if x["uid"] == uid]
    if not current:
        raise HTTPException(status_code=404, detail="no such item")
    item = current[0]
    ics = caldav.build_vtodo(uid, item["summary"],
                             description=item.get("description") or "", completed=True)
    caldav.put_todo(collection, uid, ics, etag=item.get("etag"))
    return {"ok": True, "uid": uid}


def _chore_fields(payload: dict, base: dict | None = None) -> dict:
    """Payload -> a raw chore dict, validated. Raises HTTPException(400).

    Everything the Upkeep tab writes goes through here, so the error you see next
    to the field is the same sentence the loader would have produced.
    """
    import upkeep
    raw = dict(base or {})
    if "title" in payload:
        raw["title"] = str(payload.get("title") or "").strip()
    if "every" in payload:
        raw["every"] = str(payload.get("every") or "").strip()
    if "time_of_day" in payload:
        raw["time_of_day"] = str(payload.get("time_of_day") or "").strip() or None
    if "est_min" in payload:
        raw["est_min"] = payload.get("est_min") or None
    if "essential" in payload:
        raw["essential"] = bool(payload.get("essential"))
    if "escalate_after" in payload:
        raw["escalate_after"] = str(payload.get("escalate_after") or "").strip() or None
    # window and days are alternatives; setting either clears the other, or the
    # chore would carry a stale constraint that days silently overrides.
    if "days" in payload or "window" in payload:
        raw.pop("days", None)
        raw.pop("window", None)
        days = payload.get("days")
        window = str(payload.get("window") or "").strip().lower()
        if days:
            raw["days"] = [str(d).strip().lower() for d in days]
        elif window and window not in ("", "any"):
            raw["window"] = window
    raw = {k: v for k, v in raw.items() if v is not None and v != ""}
    try:
        return upkeep._check(raw, 0)
    except upkeep.ChoreError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/upkeep")
async def upkeep_create(request: Request,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Add a chore. The id is derived from the title once and then fixed."""
    _require_auth(authorization, session)
    import upkeep
    payload = await request.json()
    chore = _chore_fields(payload)
    chores = upkeep.load()
    if any(c["slug"] == chore["slug"] for c in chores):
        raise HTTPException(status_code=409,
                            detail=f"a chore with id {chore['slug']!r} already exists")
    upkeep.save(chores + [chore])
    return {"ok": True, "slug": chore["slug"], "title": chore["title"]}


@app.post("/upkeep/{slug}/cadence")
async def upkeep_cadence(slug: str, request: Request,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Change one chore. Only the fields given are touched.

    The id is never rewritten from a new title: upkeep_state is filed under it,
    so renaming the title has to be free. The header comments are preserved and
    only the chores list below them is regenerated.
    """
    _require_auth(authorization, session)
    import upkeep
    payload = await request.json()
    chores = upkeep.load()
    target = next((c for c in chores if c["slug"] == slug), None)
    if target is None:
        raise HTTPException(status_code=404, detail="no such chore")
    base = {"id": target["slug"], "title": target["title"], "every": target["every"]}
    for key in ("window", "days", "time_of_day", "est_min", "escalate_after"):
        if target.get(key):
            base[key] = target[key]
    if target.get("essential"):
        base["essential"] = True
    updated = _chore_fields(payload, base)
    updated["slug"] = slug
    upkeep.save([updated if c["slug"] == slug else c for c in chores])
    return {"ok": True, "slug": slug, "every": updated["every"]}


@app.delete("/upkeep/{slug}")
async def upkeep_delete(slug: str,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Stop tracking a chore. Its upkeep_state row is left alone.

    Nothing is deleted from the database -- and because all_states() iterates the
    YAML rather than the table, the orphaned row cannot bring the chore back.
    """
    _require_auth(authorization, session)
    import upkeep
    chores = upkeep.load()
    if not any(c["slug"] == slug for c in chores):
        raise HTTPException(status_code=404, detail="no such chore")
    upkeep.save([c for c in chores if c["slug"] != slug])
    return {"ok": True, "slug": slug}


@app.post("/upkeep/{slug}/done")
async def upkeep_done(slug: str, request: Request,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Record that a chore was done, whether or not it was ever scheduled.

    Without this there is no way at all to log a chore you did spontaneously: the
    phone list is gone, so last_done would only ever move when a scheduled
    instance was ticked.
    """
    _require_auth(authorization, session)
    import policy, project, store, upkeep
    try:
        payload = await request.json()
    except Exception:                                        # noqa: BLE001
        payload = {}
    when = str(payload.get("on") or "").strip() or None
    conn = store.connect()
    try:
        if upkeep.by_slug(slug) is None:
            raise HTTPException(status_code=404, detail="no such chore")
        day = policy.local_today()
        upkeep.record_done(conn, slug, when or day)
        inst = upkeep.instance_for(conn, slug, day)
        if inst is not None and inst["status"] != "completed":
            project.set_state(conn, inst["id"], "done")
        row = upkeep.state_row(conn, slug)
        return {"ok": True, "slug": slug, "last_done": row["last_done"]}
    finally:
        conn.close()


@app.post("/upkeep/{slug}/snooze")
async def upkeep_snooze(slug: str, request: Request,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Stop offering a chore until a date. `days` or an explicit `until`."""
    _require_auth(authorization, session)
    from datetime import timedelta
    import policy, store, upkeep
    payload = await request.json()
    conn = store.connect()
    try:
        if upkeep.by_slug(slug) is None:
            raise HTTPException(status_code=404, detail="no such chore")
        until = str(payload.get("until") or "").strip()
        if not until:
            try:
                days = max(1, min(int(payload.get("days") or 3), 365))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="days must be a number")
            until = (policy.local_today() + timedelta(days=days)).isoformat()
        try:
            day = upkeep.snooze(conn, slug, until)
        except upkeep.ChoreError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "slug": slug, "snooze_until": day.isoformat()}
    finally:
        conn.close()


@app.post("/upkeep/{slug}/schedule")
async def upkeep_schedule(slug: str, request: Request,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Put a chore on today at a time, without going through the planner."""
    _require_auth(authorization, session)
    from datetime import timedelta
    import board as bd, caldav, policy, store, upkeep
    payload = await request.json()
    conn = store.connect()
    try:
        chore = upkeep.by_slug(slug)
        if chore is None:
            raise HTTPException(status_code=404, detail="no such chore")
        now = policy.local_now()
        try:
            task_id = upkeep.materialise(conn, chore, str(payload.get("at") or ""),
                                         day=now.date(), why="scheduled by hand")
        except upkeep.ChoreError as e:
            raise HTTPException(status_code=400, detail=str(e))
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        when = max(bd.intended_alarm(row) or now, now + timedelta(minutes=1))
        try:
            bd.write_task(conn, row, alarm_at=when)
        except caldav.CalDavError as e:
            print(f"[upkeep] schedule {slug}: {e}", flush=True)
        return {"ok": True, "slug": slug, "task_id": task_id,
                "scheduled_at": row["scheduled_at"]}
    finally:
        conn.close()


@app.post("/upkeep/retire")
async def upkeep_retire(dry: bool = True,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """One-shot: export the old Upkeep collection, mine last_done, then delete it.

    An endpoint rather than a host script because CALDAV_URL is
    http://radicale:5232 -- only reachable from inside the compose network.

    Order matters and the export is the archive, so the delete refuses unless the
    export actually wrote something. `dry=true` (the default) reports exactly what
    it would do and writes nothing.
    """
    _require_auth(authorization, session)
    import json as _json
    import caldav, store, upkeep
    from datetime import datetime as _dt

    collection = "upkeep"
    try:
        items = caldav.list_todos(collection)
    except caldav.CalDavError as e:
        return {"ok": True, "note": f"no upkeep collection to retire ({e})"}

    # Mine the most recent completion per title. iOS writes one VTODO instance
    # per completion with a random uid, which the brain never read -- so this is
    # the only record of when anything was actually last done.
    mined: dict[str, str] = {}
    today = store.datetime.now().date()
    for item in items:
        title = store.normalise_title(item.get("summary") or "")
        stamp = caldav.parse_dt(item.get("completed_at") or "")
        if not title or stamp is None or stamp.date() > today:
            continue
        got = stamp.date().isoformat()
        if title not in mined or got > mined[title]:
            mined[title] = got

    conn = store.connect()
    try:
        chores = upkeep.load()
        seeds = {c["slug"]: mined[store.normalise_title(c["title"])]
                 for c in chores if store.normalise_title(c["title"]) in mined}
        strays = [dict(r) for r in conn.execute(
            "SELECT id, title, source_key, lane FROM tasks WHERE archived=0 AND "
            "(caldav_collection='upkeep' OR lane='upkeep' OR source_key LIKE 'chore:%')")]
        plan = {"items": len(items), "mined_titles": mined, "would_seed": seeds,
                "db_rows_to_clean": strays}
        if dry:
            return {"ok": True, "dry_run": True, **plan}

        archive_dir = store.BRAIN_ROOT / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        path = archive_dir / f"upkeep-{_dt.now():%Y%m%d-%H%M%S}.json"
        path.write_text(_json.dumps(
            {"exported_at": store.now_iso(), "collection": collection,
             "items": items, "mined_last_done": mined}, indent=2, default=str),
            encoding="utf-8")
        if not items:
            return {"ok": False, "detail": "export wrote no items; refusing to delete",
                    "archive": str(path)}

        for slug, day in seeds.items():
            upkeep._touch_state(conn, slug, seed_done=day)
            upkeep.record_done(conn, slug, day)
        # Nudge rows are spent: the mechanism that made them is gone.
        conn.execute("UPDATE tasks SET archived=1, updated_at=? WHERE archived=0 "
                     "AND source_key LIKE 'chore:%'", (store.now_iso(),))
        # Anything of ours parked in that collection loses its card pointer so the
        # next audit re-pushes it to the lane it actually belongs to.
        conn.execute("UPDATE tasks SET caldav_collection=NULL, caldav_etag=NULL "
                     "WHERE caldav_collection='upkeep'")
        conn.execute("UPDATE tasks SET lane='backlog', updated_at=? WHERE archived=0 "
                     "AND lane='upkeep'", (store.now_iso(),))
        caldav.delete_collection(collection)
        return {"ok": True, "archive": str(path), "seeded": seeds,
                "cleaned": len(strays), "deleted_collection": collection}
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


@app.get("/upcoming", response_class=PlainTextResponse)
async def upcoming(authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Read-only: what is armed, what has escalated, where every chore stands."""
    _require_auth(authorization, session)
    import policy, store, upkeep

    now = policy.local_now()
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
            slot = f"  @{r['scheduled_at'][11:16]}" if r["scheduled_at"] else ""
            lines.append(f"  {when[5:16]}  [{r['track']:<7}] {r['title'][:40]}{slot}{lvl}{late}{dis}")

        lines += ["", "CHORES  (cadence from upkeep.yaml, last done from the brain)"]
        chores, errors = upkeep.load_with_errors()
        if not chores and not errors:
            lines.append("  (none yet — add them on the Upkeep tab)")
        for c in upkeep.all_states(conn, now.date()):
            mark = "essential" if c.get("essential") else "         "
            if c["state"] == upkeep.DUE:
                state = f"DUE  overdue {c['overdue_days']}d"
                if c.get("missed_windows"):
                    state += f", deferred {c['missed_windows']}x"
            elif c["state"] == upkeep.WAITING:
                state = f"waiting for its window, next {c['next_due']}"
            elif c["state"] == upkeep.SNOOZED:
                state = f"snoozed until {c['snooze_until']}"
            elif c["state"] == "broken":
                state = f"BROKEN: {c.get('error')}"
            else:
                state = f"next {c['next_due']}"
            last = f"  last {c['last_done']}" if c.get("last_done") else "  never done"
            lines.append(f"  {c['title'][:28]:<28} every {str(c['every']):<4} {mark}  {state}{last}")
            if c.get("scheduled_today"):
                at = (c["scheduled_today"].get("at") or "")[11:16]
                lines.append(f"       on today at {at} ({c['scheduled_today']['status']})")
        for e in errors:
            lines.append(f"  !! {e}")
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


# ------------------------------------------------------------- planning ---
# Today's action items. Generated only when asked -- there is no schedule,
# because a plan waiting for you since 06:30 is a plan you have already
# learned to ignore.
#
# The model runs on the host (that is where the subscription credential is),
# so these endpoints are a queue: the phone creates a job, a host worker
# claims it, and the result comes back here to be VALIDATED before anything
# is written. plan.validate() is the gate; the worker never touches the board.

@app.get("/plan/today.json")
async def plan_today_json(authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Everything the Today tab needs, including WHICH DAY the plan is for.

    The tab used to decide "is a plan in motion?" by asking whether the today
    lane was non-empty. Unfinished items stay in that lane by design, so the
    morning after a plan it still looked like the plan was current and the
    planning screen never came back. A day is a date, not a row count.
    """
    _require_auth(authorization, session)
    import datetime as _dt
    import store
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT applied_at FROM plan_jobs WHERE applied_at IS NOT NULL"
            " ORDER BY applied_at DESC LIMIT 1").fetchone()
        planned_on = row["applied_at"][:10] if row else None
        import policy as _policy
        today_str = _policy.local_today().isoformat()

        def slim(r):
            is_upkeep = str(r["source_key"] or "").startswith("upkeep:")
            return {"id": r["id"], "title": r["title"], "lane": r["lane"],
                    "blocked_on": r["blocked_on"], "project": r["project"],
                    "is_upkeep": is_upkeep,
                    "at": (r["scheduled_at"] or "")[11:16] or None,
                    "estimate_min": r["estimate_min"],
                    # A chore instance is created for one day and archived if it
                    # outlives it, so it can never be "carried from" an earlier
                    # day -- labelling it that way would be a lie on the screen.
                    "carried_from": (r["updated_at"][:10]
                                     if r["lane"] == "today" and not is_upkeep
                                     and planned_on != today_str
                                     else None)}

        rows = conn.execute(
            "SELECT * FROM tasks WHERE archived=0 AND status!='completed'"
            " AND lane IN ('today','in_progress','backlog')"
            # Slotted things first, in time order: a day with "laundry at 19:00"
            # in it reads as a schedule. today_seq preserves the order the plan
            # was accepted in; `seq` is project ordering and is not ours to use.
            " ORDER BY scheduled_at IS NULL, scheduled_at,"
            " today_seq IS NULL, today_seq, seq, score DESC"
        ).fetchall()
        # A finished plan you have neither accepted nor discarded. It used to
        # exist only in the page's memory, so a reload or a dropped connection
        # threw it away while the job sat safe in the database.
        import plan as _plan

        # A plan you never accepted is stale the next morning: it was written
        # against a board that has moved on, and its prose describes tasks that
        # may now be done. Retire it here rather than letting it hijack the page
        # forever -- which is exactly what happened, for five days, with a draft
        # that was not even his. The typed prompt stays in plan_jobs.message.
        if _plan.retire_stale_pending(conn, today_str):
            conn.commit()

        # A request still in flight. Reported separately from `pending` so the
        # page can wait for it rather than offering to start another.
        wjob = _plan.working_job(conn, today_str)
        working = None if wjob is None else {
            "id": wjob["id"], "kind": wjob["kind"], "created_at": wjob["created_at"]}

        pend = _plan.pending_job(conn)

        pending = None
        if pend is not None:
            # Re-validate against the board NOW rather than showing the
            # snapshot taken when the plan was written. A draft left overnight
            # listed tasks that had since been completed -- and worse, listed
            # them as things it was about to change. What is displayed has to
            # be what apply would actually do, and apply already re-validates.
            if pend["kind"] == "update":
                prop, stale = _plan.validate_update(conn, pend["raw"] or "")
            else:
                prop, stale = _plan.validate(conn, pend["raw"] or "")
            # Notes are the model's prose ABOUT the proposal. If part of the
            # proposal failed re-validation the prose describes something that
            # is no longer true, and prose cannot be re-validated -- so it is
            # withheld rather than shown alongside a contradiction.
            if stale:
                prop = dict(prop, notes="")
            pending = {
                "id": pend["id"], "kind": pend["kind"],
                "created_at": pend["created_at"],
                "from_today": True,          # guaranteed by the retirement loop
                "message": pend["message"] or "",
                "proposal": prop,
                "dropped": len(stale),       # a count for the page
                "rejected": stale,           # detail, for debugging only
            }

        # Chores due today, so the planning screen can offer them first. Read
        # from the engine, never from a lane -- there is no upkeep lane any more.
        import upkeep as _upkeep
        try:
            states = _upkeep.all_states(conn)
            upkeep_due = [
                {"slug": c["slug"], "title": c["title"], "every": c["every"],
                 "overdue_days": c["overdue_days"], "last_done": c["last_done"],
                 "missed_windows": c["missed_windows"],
                 "est_min": c.get("est_min"), "time_of_day": c.get("time_of_day")}
                for c in states
                if c["state"] == _upkeep.DUE and not c.get("scheduled_today")]
            upkeep_waiting = [
                {"slug": c["slug"], "title": c["title"], "next_due": c["next_due"],
                 "overdue_days": c["overdue_days"]}
                for c in states if c["state"] == _upkeep.WAITING]
        except Exception as e:                               # noqa: BLE001
            print(f"[plan] upkeep states: {type(e).__name__}: {e}", flush=True)
            upkeep_due, upkeep_waiting = [], []

        return {
            "date": today_str,
            "planned_on": planned_on,
            "is_current": planned_on == today_str,
            "working": working,
            "pending": pending,
            "upkeep_due": upkeep_due,
            "upkeep_waiting": upkeep_waiting,
            "today": [slim(r) for r in rows if r["lane"] == "today"],
            "pool": [slim(r) for r in rows if r["lane"] != "today"],
        }
    finally:
        conn.close()


@app.post("/plan/draft")
async def plan_draft(payload: dict | None = None,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Ask for a plan. Optionally with tasks you already ticked and context.

    Three shapes, all the same call: nothing (you decide), ticked tasks only
    (order these), or ticked tasks plus text (these, and here is what else
    matters). One button on the phone, one endpoint here.
    """
    _require_auth(authorization, session)
    import store, plan
    payload = payload or {}
    selected = [str(x) for x in (payload.get("selected") or [])][:50]
    message = str(payload.get("message", "")).strip()
    conn = store.connect()
    try:
        ctx = plan.build_context(conn, selected=selected, message=message)
        jid = plan.create_job(conn, "draft",
                              {"system": plan.SYSTEM, "user": json.dumps(ctx)},
                              message=message)
        conn.commit()
        return {"job": jid}
    finally:
        conn.close()


@app.post("/plan/revise")
async def plan_revise(payload: dict,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Argue with a draft. The board is re-read, not reused from the parent."""
    _require_auth(authorization, session)
    import store, plan
    parent_id = str(payload.get("parent", ""))
    message = str(payload.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    conn = store.connect()
    try:
        parent = plan.get_job(conn, parent_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="no such job")
        ctx = plan.revise_context(conn, parent, message)
        jid = plan.create_job(conn, "revise",
                              {"system": plan.REVISE_SYSTEM, "user": json.dumps(ctx)},
                              message=message, parent=parent_id)
        conn.commit()
        return {"job": jid}
    finally:
        conn.close()


@app.post("/plan/update")
async def plan_update(payload: dict,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Tell the plan what happened. Deltas only -- this never re-plans.

    Separate from /plan/revise on purpose. Revise argues with a draft that has
    not been committed; update changes a day already in motion. Sharing one
    endpoint would mean one prompt trying to do both, and the failure mode of
    that is a finished task quietly reshuffling the afternoon.
    """
    _require_auth(authorization, session)
    import store, plan
    message = str(payload.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    conn = store.connect()
    try:
        ctx = plan.update_context(conn, message)
        jid = plan.create_job(conn, "update",
                              {"system": plan.UPDATE_SYSTEM, "user": json.dumps(ctx)},
                              message=message)
        conn.commit()
        return {"job": jid}
    finally:
        conn.close()


@app.get("/plan/job/{job_id}")
async def plan_job(job_id: str,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Poll a job. The UI spins on this while the model runs."""
    _require_auth(authorization, session)
    import store, plan
    conn = store.connect()
    try:
        row = plan.get_job(conn, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such job")
        return {
            "id": row["id"], "kind": row["kind"], "state": row["state"],
            "message": row["message"], "parent": row["parent"],
            "error": row["error"], "applied_at": row["applied_at"],
            "proposal": json.loads(row["proposal"]) if row["proposal"] else None,
            "rejected": json.loads(row["rejected"]) if row["rejected"] else [],
        }
    finally:
        conn.close()


@app.get("/plan/next")
async def plan_next(authorization: str | None = Header(default=None)):
    """Worker endpoint: claim the oldest pending job. Bearer token only."""
    expected = f"Bearer {TOKEN}"
    if not (authorization and secrets.compare_digest(authorization, expected)):
        raise HTTPException(status_code=401, detail="bad token")
    import store, plan
    conn = store.connect()
    try:
        row = plan.claim_job(conn)
        conn.commit()
        if row is None:
            return {}
        return {"id": row["id"], "kind": row["kind"],
                "context": json.loads(row["context"])}
    finally:
        conn.close()


@app.post("/plan/job/{job_id}/result")
async def plan_result(job_id: str, payload: dict,
        authorization: str | None = Header(default=None)):
    """Worker endpoint: hand back the model's reply.

    Validation happens HERE, not in the worker. The worker is on the host and
    is deliberately dumb; letting it decide what counts as a valid plan would
    move the gate outside the process that owns the database.
    """
    expected = f"Bearer {TOKEN}"
    if not (authorization and secrets.compare_digest(authorization, expected)):
        raise HTTPException(status_code=401, detail="bad token")
    import store, plan
    conn = store.connect()
    try:
        row = plan.get_job(conn, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such job")
        if payload.get("error"):
            plan.fail_job(conn, job_id, str(payload["error"]))
            conn.commit()
            return {"ok": True, "state": "failed"}
        raw = str(payload.get("raw", ""))
        # An update is validated against a different, wider op set and is not
        # allowed to emit a day. Dispatching here keeps that decision next to
        # the database rather than in the host worker.
        if row["kind"] == "update":
            proposal, rejected = plan.validate_update(conn, raw)
        else:
            proposal, rejected = plan.validate(conn, raw)
        plan.finish_job(conn, job_id, raw, proposal, rejected)
        conn.commit()
        return {"ok": True, "state": "done",
                "today": len(proposal.get("today", [])),
                "mutations": len(proposal.get("mutations", [])),
                "rejected": len(rejected)}
    finally:
        conn.close()


@app.post("/plan/job/{job_id}/dismiss")
async def plan_dismiss(job_id: str,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Throw a plan away without applying it, so it stops resurfacing."""
    _require_auth(authorization, session)
    import store, plan
    conn = store.connect()
    try:
        if plan.get_job(conn, job_id) is None:
            raise HTTPException(status_code=404, detail="no such job")
        conn.execute("UPDATE plan_jobs SET dismissed_at=?, updated_at=? WHERE id=?",
                     (store.now_iso(), store.now_iso(), job_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/plan/apply")
async def plan_apply(payload: dict, background: BackgroundTasks,
        authorization: str | None = Header(default=None),
        session: str | None = Cookie(default=None, alias=auth.COOKIE)):
    """Commit a draft. Nothing before this point has changed the board."""
    _require_auth(authorization, session)
    import store, plan
    job_id = str(payload.get("job", ""))
    conn = store.connect()
    try:
        row = plan.get_job(conn, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such job")
        if row["state"] != "done" or not row["proposal"]:
            raise HTTPException(status_code=409, detail="job is not ready")
        if row["applied_at"]:
            raise HTTPException(status_code=409, detail="already applied")
        if row["dismissed_at"]:
            raise HTTPException(status_code=409, detail="already discarded")
        # A plan is for a day. Applying yesterday's would move yesterday's picks
        # onto today and, worse, schedule chores under today's date from a
        # proposal written against a board that has since moved on.
        import policy as _policy
        if (row["created_at"] or "")[:10] != _policy.local_today().isoformat():
            raise HTTPException(
                status_code=409,
                detail="that plan was made on a different day -- ask for a new one")

        # Re-validate against the board as it is NOW, not as it was when the
        # plan was written. A draft can sit for minutes or days, and in between
        # you may have ticked something off on your phone -- active_tasks
        # excludes completed and archived rows, so a stale entry is dropped
        # here rather than resurrecting a finished task onto today. Cheap
        # insurance, and the raw reply is stored precisely so this is possible.
        if row["kind"] == "update":
            proposal, stale = plan.validate_update(conn, row["raw"] or "")
        else:
            proposal, stale = plan.validate(conn, row["raw"] or "")
        # `accept` is a set of stable ids: a task id for a today pick, a
        # content key for a mutation. It used to be positions for mutations,
        # which broke the moment apply started re-validating -- dropping an
        # entry shifted every position after it, so unticking one change could
        # discard a different one. Anything not listed is simply not applied.
        keep = payload.get("accept")
        if isinstance(keep, list):
            kept = {str(k) for k in keep}
            proposal["mutations"] = [m for m in proposal.get("mutations", [])
                                     if m.get("key") in kept]
            if "today" in proposal:
                proposal["today"] = [i for i in proposal["today"]
                                     if i["task_id"] in kept]
        if row["kind"] == "update":
            result = plan.apply_update(conn, proposal)
        else:
            result = plan.apply(conn, proposal)
        conn.execute("UPDATE plan_jobs SET applied_at=?, updated_at=? WHERE id=?",
                     (store.now_iso(), store.now_iso(), job_id))
        conn.commit()
    finally:
        conn.close()
    # Project it to the phone now rather than at the next timer tick: you just
    # decided what today is, and the list should exist before you put the
    # phone down.
    background.add_task(run_triage_once)
    return {"ok": True, "stale": stale, **result}
