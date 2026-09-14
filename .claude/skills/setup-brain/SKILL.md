---
name: setup-brain
description: Set up or customise the Focus Board Brain on a self-hosted server — installs the stack, wires CalDAV to Apple Reminders, configures chores, quiet hours and LLM providers. Use when the user wants to install this project, change their chore list or working hours, add an LLM provider, or debug why reminders are not arriving.
---

# Setting up the Focus Board Brain

A task system that catches what its owner dumps at it, routes it by what kind of
thing it is, and pushes it to Apple Reminders as real on-device alarms.

Work through this with the user. **Ask before running anything that writes to
their board or their database** — the whole point is that they trust what is on
it.

## What the pieces are

| Container | Does |
|---|---|
| `brain` | FastAPI. Capture, triage, escalation, the web UI |
| `litellm` | One OpenAI-compatible endpoint in front of several free LLM providers |
| `radicale` | Self-hosted CalDAV. The `.ics` files Apple Reminders syncs from |
| `syncthing` | Optional. Shares project context folders to other machines |

SQLite at `$BRAIN_ROOT/state/brain.db` is the system of record. Reminders is a
projection of it — never the other way round.

## Order of setup

Do these in order; each one fails confusingly if the previous is skipped.

### 1. Prerequisites
- rootless `podman` + `podman compose` (or Docker)
- A directory for data, ideally on redundant storage. `BRAIN_ROOT` in compose.
- An iPhone, for the Reminders side. The CLI works without one.

### 2. Secrets
```
cp .env.example .env && chmod 600 .env
cp litellm.env.example litellm.env && chmod 600 litellm.env
```
Fill both in. `LITELLM_MASTER_KEY` must be **identical** in the two files.
Get free LLM keys from the providers listed at https://freellm.net — Groq and
Google AI Studio are the highest-value pair and neither needs a card.

### 3. Model IDs — check, do not copy
`litellm.config.yaml` pins specific models. **Free-tier catalogues churn fast.**
Verify before trusting them:
```
curl -s -H "Authorization: Bearer $GROQ_API_KEY" https://api.groq.com/openai/v1/models
curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY"
```
Two traps seen in practice: a model can be listed but 404 for new keys, and
Groq rejects Python's default urllib User-Agent while accepting curl with the
same key.

### 4. CalDAV, then TLS
`radicale/users` is `username:password`. Set `htpasswd_encryption = bcrypt` in
`radicale/config` for anything other than a throwaway test.

**iOS refuses plain HTTP for CalDAV.** "Use SSL off" does not work. You need a
real certificate. With Tailscale:
```
sudo tailscale serve --bg --https=443 http://127.0.0.1:5232
```
That gives a publicly trusted cert with no files to manage.

### 5. Add the account on the phone
Settings → Calendar → Accounts → Add Account → Other → **Add CalDAV Account**.
Use the hostname from `tailscale serve status`, SSL on, no port. Turn
**Reminders** on for the account; Calendars can stay off.

**If it says "cannot connect using SSL"**, it is usually DNS, not TLS —
`*.ts.net` names resolve only through Tailscale's own resolver, and iOS reports
that failure as an SSL error. Check "Use Tailscale DNS" is on.

### 6. Prove alarms fire before building on them
```
./brain.sh upkeep      # writes the recurring chores
```
Then set a chore a few minutes out and wait. If the phone does not buzz, the
whole on-device alarm design is wrong for that setup and the user should know
before relying on it.

## Customising

**Chores** — `upkeep.yaml`. `at`, `repeat` (daily / weekly / monthly /
`every N weeks`), `day` for weekly ones, `essential: true` for the few that
should chase the user when overdue. Spell it `day`, not `on`: YAML treats `on`
as a boolean and the weekday silently vanishes. Re-run `./brain.sh upkeep`.

**Quiet hours and tracks** — `config.yaml`. `downtime` is absolute and beats
every per-track window. Reminders coming due inside a closed window are held
until it opens, never dropped.

**Priority** — is computed, not a field. One stored input (`consequence`:
breaks / costs / improves / optional) and everything else derived from
deadline, age, blocking and estimate. See `score.py`.

## Design rules worth preserving

Change these only deliberately — each exists because the obvious alternative
failed in practice:

- **Capture never fails.** `/capture` appends to a plaintext file and returns
  before any model sees it. No LLM in the write path.
- **The model proposes, code decides.** Every LLM response is schema-validated
  before anything is written. A bad response wastes a cycle; it cannot corrupt
  the board.
- **Nothing is deleted, only archived.** This has already saved the project once.
- **The user always wins.** Tick, drag or rename on the phone and the database
  follows. Deleting a reminder dismisses the nudge; it does NOT delete the task.
- **Track everything, surface a little.** The 3-per-project cap controls what is
  loud, never what is tracked.
- **Escalation adds precision, not volume.** It stops after four attempts —
  endless nagging trains people to swipe without reading.

## Debugging

```
./brain.sh doctor        # containers, CalDAV, pending captures, board tail
./brain.sh upcoming      # armed alarms, escalation levels, overdue chores
./brain.sh logs 40       # brain logs; add a container name for another
podman logs radicale | grep PUT
```

That last one settles most "who changed this?" questions instantly: the brain
writes as `focus-board-brain/0.1`, an iPhone as `iOS/... remindd/...`.
