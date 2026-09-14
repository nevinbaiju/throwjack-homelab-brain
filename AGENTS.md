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
    upkeep.py    recurring chores (RRULE). NOT tasks — see below
    escalate.py  re-arming alarms, and nudges for overdue essential chores
    score.py     priority: one stored input, everything else derived
    policy.py    quiet hours
    store.py     SQLite — the system of record
    auth.py      scrypt password hashing, signed session cookies

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
- **Chores are not tasks.** Recurring `u_` items live in the Upkeep list; iOS
  owns their recurrence and completion. The brain reads them, never writes to
  them. Holding a chore as a task fights iOS's recurrence handling.
- **Escalation adds precision, not volume.** The text gets more specific; the
  notification count does not climb, and it stops after four attempts.
- **Track everything, surface a little.** Caps control what is loud, never what
  is tracked.

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

There is no test runner checked in; tests were written per-change against a
scratch database and scratch CalDAV collections. If you add tests, keep that
property: **a test must never touch the real board or the real database.**
