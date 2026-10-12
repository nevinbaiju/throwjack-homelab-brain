# Focus Board Brain — for coding agents

You are helping someone set up or modify this project. It is a personal task
system: it catches what its owner dumps at it, works out what kind of thing it
is, and pushes it to Apple Reminders as real on-device alarms.

It was built for someone with ADHD. Most of the unusual decisions exist because
the obvious alternative made things worse in practice, not because nobody
thought of the obvious one. Read "Design rules" before changing behaviour.

## Orientation

    app.py       HTTP surface: capture, triage trigger, web UI, auth
    triage.py    the worker loop — reconcile, drain, triage, escalate, rescore
    llm.py       LiteLLM client + strict schema validation of model output
    board.py     the CalDAV projection: lanes, reconcile, inbox drain
    caldav.py    hand-rolled CalDAV/iCalendar client
    upkeep.py    recurring chores: the due engine, state and overnight sweep.
                 Imports no CalDAV on purpose, so it is testable on the host
    escalate.py  re-arming alarms, and scheduling badly-overdue essential chores
    score.py     priority: one stored input, everything else derived
    policy.py    quiet hours
    store.py     SQLite — the system of record
    auth.py      scrypt password hashing, signed session cookies
    bin/brain-planner    host side: runs the planning model on the host's Claude login
    bin/brain-followups  host side: files what Claude Code conversations left open
                         into the Homelab Backlog queue (POST /homelab)

Apple Reminders is a *projection* of SQLite, never the source of truth.

## Setup

`.claude/skills/setup-brain/SKILL.md` is the full walkthrough — installation,
CalDAV, TLS, the phone, and the traps. Claude Code users: run `/setup-brain`.
Other tools: read that file, it is plain markdown.

Short version:

    cp .env.example .env && chmod 600 .env
    cp litellm.env.example litellm.env && chmod 600 litellm.env
    # fill both in; LITELLM_MASTER_KEY must match across them
    podman compose up -d --build
    ./brain.sh setpass
    ./brain.sh upkeep

## Design rules — change these only on purpose

- **Capture never fails.** `/capture` appends to a plaintext file and returns
  before any model runs. Never put an LLM, a database write, or a network call
  in that path.
- **The model proposes, code decides.** Every LLM response is validated against
  a schema before anything is written. Never let a model issue a write directly.
- **Nothing is deleted, only archived.** Both the database and the board.
- **The user always wins.** If they tick, drag or rename something on their
  phone, the database follows them. Never revert their change.
- **Two gestures, two meanings.** Ticking a reminder completes the task.
  *Deleting* one dismisses the nudge and leaves the task alone.
- **Chores are policy plus state, not standing cards.** `upkeep.yaml` holds the
  cadence; the `upkeep_state` table holds `last_done`. A chore becomes a real
  task only for the day it is scheduled, and that instance is **archived** if the
  day ends without it done — so not doing one costs nothing and it simply comes
  back. The brain owns the recurrence; iOS owns nothing.
  This used to be the opposite ("chores are not tasks, iOS owns them"), and it
  was wrong for one reason: iOS advances a recurrence from the previous DUE date,
  so finishing a fortnightly chore four days late made the next one due in ten
  days rather than fourteen. Cadence has to be measured from what you actually
  did. Do not put it back.
- **Due is `last_done + every`.** Never a fixed weekday. A chore with a `window`
  that misses its window waits for the next one instead of nagging on a day it
  cannot be done.
- **Escalation adds precision, not volume.** The text gets more specific; the
  notification count does not climb, and it stops after four attempts. For a
  chore it means *scheduling* it into today, not raising a second card about it.
- **Two gestures, two meanings, for a chore too.** Ticking a chore instance
  completes it and moves `last_done`. *Deleting* the card means "not today": the
  instance is archived and the chore stays due, which is the same outcome as the
  overnight sweep. `board.audit` must keep honouring `dismissed_until`, or a
  swipe is silently undone within ten minutes.
- **Track everything, surface a little.** Caps control what is loud, never what
  is tracked. There is no cap on Today and no work-in-progress cap: a number the
  system nags about but never enforces is noise you learn to ignore, and it costs
  the real warnings their credibility.
- **Today is the only active lane.** `in_progress` was merged into it — they were
  one answer held in two lists. Work started and unfinished stays on Today and
  surfaces in the next day's planner as carried over. Nothing auto-promotes into
  it; the planner is the only thing that puts work on a day. `project.promote` was
  deleted rather than capped, because with Today as the active lane it would have
  overwritten the day's decision every ten minutes.
- **A lane can be off-board.** `board.OFF_BOARD` lists lanes the brain tracks
  but never projects to the phone. `homelab` is one: a review queue of loose ends
  that `bin/brain-followups` files, which are unreviewed machine output — pushing
  them to Reminders before they have been looked at is the noise this system
  exists to suppress. Promoting one is what puts it on the phone. The guards are
  not optional: `audit()` walks *every* active row and `rescore_and_sync()` pushes
  whatever has no matching card, so a lane that is merely absent from `LANES`
  makes the first item filed there raise `KeyError` out of `collection_for()` —
  which no `except caldav.CalDavError` catches, so it kills reconcile, the inbox
  drain and triage. That crash has now been designed around three times.
- **A card you create on your phone becomes a task.** reconcile adopts any item
  in Today or Backlog whose uid matches no row, keeping the iPhone's own UUID as
  the task id. Triage then supplies track, consequence and project — never the
  lane and never the title. Rewriting text the user is looking at is the one
  thing adoption must not do, which is the same carve-out the `meta` track has.
  Done is not adopted: ticking is how a completion is recorded.

## Things that have actually broken

Worth knowing before you spend an evening on them:

- iOS **refuses plain HTTP** for CalDAV, and reports a DNS failure as an SSL
  error. TLS is not optional.
- Free LLM model IDs **churn**. Always query the provider's `/models` endpoint
  rather than trusting a config file, including this one.
- Groq **403s Python's default urllib User-Agent** while accepting curl with the
  same key. Set a User-Agent.
- `docker-compose` **interpolates `$` in env_file values**, so a `$` inside a
  password hash is silently eaten. That is why the hash separator is `:`.
- YAML treats **`on` as a boolean**, so `on: friday` loses the weekday. Use `day`.
- Never run destructive tests against a live board. Delete only ids the test
  created, never "everything that looks managed".

## Testing

    bash tests/run            # host: scratch sqlite, no network, no CalDAV
    bash tests/run-in-brain   # the endpoint tests, inside the container

`tests/run` is where new work belongs: `upkeep.py` deliberately imports neither
`board` nor `caldav`, so the entire due engine, state machine and overnight sweep
run there in under a second. Endpoint tests that need FastAPI go in
`tests/run-in-brain`, which refuses to start unless `BRAIN_ROOT` is under `/tmp`.

Keep the property that matters: **a test must never touch the real board or the
real database.** Stub `board.write_task` / `board.remove_task` rather than letting
a test reach Radicale — a test in this tree once PUT to the live server and was
saved only by a 401.
