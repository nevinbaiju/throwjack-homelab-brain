# Focus Board Brain

A self-hosted task system that catches whatever you throw at it, works out what
kind of thing it is, and puts it on your phone as a real reminder.

Built for ADHD. The design goal is not "more features" — it is that nothing you
think of gets lost, and that the system is loud about the few things that matter
rather than quietly accurate about everything.

## What it does

**Capture never fails.** Type into a web page, say it to Siri, run a CLI
command, or have a coding agent write a file. Every route appends to a plaintext
log on disk *before* any model sees it. If every LLM provider is down, your
thought is still safe.

**Triage decides what kind of thing it is.** A local LLM call turns
`"we're out of dish soap"` into an upkeep task, `"p1: qz-7 rebase"` into a work
item you priced yourself, and `"sort out the thesis"` into a concrete first step.

**Your phone gets a real reminder.** Tasks become items in Apple Reminders over
self-hosted CalDAV, with alarms that fire **on the device** — no push service,
works offline, mirrors to your Watch.

**It notices what you dropped.** A missed reminder re-arms on a growing backoff.
An essential chore that goes badly overdue raises its own nudge. Both stop after
four attempts, because endless nagging just teaches you to swipe.

## Why it looks like this

Six lists, five of them a lie you tell yourself. The rules that survived contact
with real use:

- **Track everything, surface a little.** A global work-in-progress cap makes
  you *forget* things. The cap here is 3 per project, it is a nudge rather than
  an eviction, and nothing ever leaves the system.
- **Priority is computed, not a field.** You set one thing — what happens if it
  never gets done — and deadline, age, blocking and effort do the rest. Priority
  fields rot; `P1` inflates until everything is `P1`.
- **Escalation adds precision, not volume.** "Have breakfast — due 9h ago"
  becomes "…has been waiting 20h. Do it, or drop it from upkeep.yaml."
- **Nothing is deleted, only archived.** This has already saved the project once.
- **You always win.** Move, rename or tick anything on your phone and the
  database follows you. Delete a reminder and it dismisses the nudge — it does
  not delete your task.

## Stack

    brain      FastAPI — capture, triage, escalation, web UI
    litellm    one endpoint in front of several free LLM providers
    radicale   self-hosted CalDAV, plain .ics on disk
    syncthing  optional — shares project context to your other machines

SQLite is the system of record. Apple Reminders is a projection of it.

Runs on rootless podman. No cloud services except the LLM calls themselves, and
those can be pointed at a local model.

## Setup

Open the repo with Claude Code or Antigravity and ask it to set the project up —
`AGENTS.md` and `.claude/skills/setup-brain/SKILL.md` walk it through
installation, CalDAV, TLS, and the traps that cost real evenings.

By hand:

    cp .env.example .env && chmod 600 .env
    cp litellm.env.example litellm.env && chmod 600 litellm.env
    # fill both in — LITELLM_MASTER_KEY must match across them
    podman compose up -d --build
    ./brain.sh setpass          # browser login
    ./brain.sh upkeep           # write your recurring chores

Free LLM keys: <https://freellm.net>. Groq and Google AI Studio are the
highest-value pair; neither needs a card.

## Daily use

    ./brain.sh capture "p2: fix the deploy script"
    ./brain.sh dump myproject notes.md    # a tangle becomes an ordered plan
    ./brain.sh board                      # everything, score-ordered
    ./brain.sh upcoming                   # what is armed, what is overdue
    ./brain.sh doctor                     # is any of this actually working

Or the web UI, which is two boxes: **File task** for one thing, **Plan task**
for a project's worth of thinking.

## Customising

- `upkeep.yaml` — your recurring chores, their cadence, and which ones are
  essential enough to chase you.
- `config.yaml` — quiet hours, per-track windows, how long things sit before
  they start climbing.
- `litellm.config.yaml` — which models, and the fallback order when a free tier
  rate-limits.

## Caveats, honestly

- **Apple flattens nested subtasks over CalDAV.** Steps are individually
  checkable items in a per-project list, not a collapsible tree. That is Apple's
  limit, not fixable here.
- **The Watch mirrors your phone**; it holds no CalDAV account of its own.
- **Free LLM tiers are free because your inputs are usually trainable.** Route
  anything sensitive to a local model, or keep it in shorthand only you can read.
- **No test suite is checked in.** Tests were written per-change against scratch
  databases. Contributions welcome, provided they never touch a live board.
