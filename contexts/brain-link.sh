#!/usr/bin/env bash
# Link a local project repo to its Focus Board Brain context.
#
# Run from inside your repo — the repo stays where it is, nothing moves into
# the brain folder:
#
#     export BRAIN_CONTEXTS=~/brain-contexts      # wherever YOU pointed Syncthing
#     brain-link.sh doordash
#     brain-link.sh doordash ~/code/some-repo
#
# BRAIN_CONTEXTS is the local path of the Syncthing-shared contexts folder on
# THIS machine. It differs per device and only you know it, so it is stated
# once rather than discovered.
#
# Creates:
#   .brain/       symlink -> the synced context folder for that project
#   AGENTS.md     the contract (Antigravity, Codex, Cursor, most agent tools)
#   CLAUDE.md     imports AGENTS.md + the live state (Claude Code)
#
# Idempotent. Existing AGENTS.md / CLAUDE.md are appended to inside a marked
# block, never overwritten, and re-running replaces only that block.
set -euo pipefail

# The contexts root comes from the environment, NOT from where this script
# happens to live. Deriving it from $0 meant every copy of the synced folder
# was a working installer pointing at ITSELF: after the server's data root
# moved, an old copy of the tree was still left behind, still contained this
# script, and linking through it wrote captures into a directory nothing read.
# Twelve of them, silently, for a whole day. There is deliberately no
# fallback -- an unset variable must stop the script, never make it guess.
CONTEXTS="${BRAIN_CONTEXTS:-}"
if [ -z "$CONTEXTS" ]; then
  echo "BRAIN_CONTEXTS is not set." >&2
  echo >&2
  echo "Set it to the local path of the Syncthing-shared brain contexts folder" >&2
  echo "on this machine, then re-run:" >&2
  echo >&2
  echo "    export BRAIN_CONTEXTS=/path/to/your/synced/contexts" >&2
  exit 1
fi
[ -d "$CONTEXTS" ] || { echo "BRAIN_CONTEXTS=$CONTEXTS is not a directory" >&2; exit 1; }
CONTEXTS="$(cd "$CONTEXTS" && pwd)"
SLUG="${1:?usage: brain-link.sh <project-slug> [repo-dir]}"
REPO="${2:-$PWD}"
SRC="$CONTEXTS/$SLUG"

[ -d "$SRC" ] || { echo "no such project context: $SRC" >&2
                   echo "available: $(ls -d "$CONTEXTS"/*/ 2>/dev/null | xargs -n1 basename | tr '\n' ' ')" >&2; exit 1; }
[ -d "$REPO" ] || { echo "no such directory: $REPO" >&2; exit 1; }
cd "$REPO"

# --- .brain symlink -------------------------------------------------------
if [ -L .brain ]; then
  ln -sfn "$SRC" .brain
elif [ -e .brain ]; then
  echo ".brain exists and is not a symlink — leaving it alone" >&2; exit 1
else
  ln -s "$SRC" .brain
fi
echo "  .brain -> $SRC"

BEGIN="<!-- BEGIN focus-board-brain -->"
END="<!-- END focus-board-brain -->"

write_block() {                      # $1 = file, $2 = body
  local file="$1" body="$2" tmp
  tmp=$(mktemp)
  if [ -f "$file" ] && grep -qF "$BEGIN" "$file"; then
    awk -v b="$BEGIN" -v e="$END" 'index($0,b){s=1} !s{print} index($0,e){s=0}' "$file" > "$tmp"
    printf '%s\n%s\n%s\n' "$BEGIN" "$body" "$END" >> "$tmp"
  else
    [ -f "$file" ] && { cat "$file" > "$tmp"; printf '\n' >> "$tmp"; }
    printf '%s\n%s\n%s\n' "$BEGIN" "$body" "$END" >> "$tmp"
  fi
  mv "$tmp" "$file"
  echo "  $file"
}

write_block AGENTS.md "## Task context (Focus Board Brain)

This repo is linked to Nevin's task system. \`.brain/\` is a symlink to the
shared context folder, synced from his home server.

**Read at the start of a session:** \`.brain/STATE.md\` — the current plan, what
is in progress, what is next. \`.brain/BRIEF.md\` — his own notes and
constraints; change it only if he asks you to. \`.brain/tasks.json\` — the same data with
task ids, if you need them.

Do not re-plan work \`STATE.md\` already lists as in progress or done.

**First, check nothing is stuck.** Ingested captures are MOVED to
\`.brain/captures/.ingested/\`. So any \`.md\` file sitting directly in
\`.brain/captures/\` that you did not just write has not reached his board.
If you find some, say so before anything else — name the files and stop. It
usually means \`.brain\` points at a folder his server is not reading, and
everything written through it is being silently discarded. Do not try to fix
it yourself.

**When something becomes actionable,** write a NEW markdown file into
\`.brain/captures/\`, one item per file, e.g.
\`.brain/captures/$(date +%Y-%m-%d)-short-slug.md\`. One or two sentences is
enough. Nevin runs a sync, which turns them into items on his task lists.

He has ADHD: write the concrete next physical action, not the topic. A task
whose first step is not obvious does not get started.
Good: \"Redo the Nyquist plots in problem set 4 before Thursday.\"
Bad:  \"TODO: various things\"

**To update an existing task,** write a capture whose FIRST line is a directive
with the task id from \`tasks.json\`:

    done: t_a3d66700d70d
    doing: t_334ca79ea71e
    blocked: t_ef1e0428e710
    drop: t_1234567890ab

Anything after the first line is kept as a note. \`done\` closes it, \`doing\`
promotes it to In Progress, \`blocked\` moves it to Waiting, \`drop\` archives it.
The id must match exactly; an unknown id is treated as an ordinary new capture,
never guessed at. This is how you report work you actually finished — without
it, nothing you do here can close a card.

Backlog steps for a project appear as ONE card listing them, so \`tasks.json\` is
where you look up ids, not the board.

**Never** edit \`.brain/STATE.md\`, \`.brain/tasks.json\` or \`.brain/CLAUDE.md\`
— the brain owns them and overwrites them. Never delete anything in
\`.brain/dumps/\`; that is his raw thinking, kept verbatim."

write_block CLAUDE.md "@AGENTS.md
@.brain/STATE.md

The project state above is imported at launch — you already have it, no need to
go read it again. The first time you load this you will be asked to approve an
import from outside the working directory; that is the \`.brain\` symlink."

# --- keep the symlink out of git -----------------------------------------
if [ -e .git ] && ! grep -qxF '.brain' .gitignore 2>/dev/null; then
  printf '\n# local symlink to task-system context\n.brain\n' >> .gitignore
  echo "  .gitignore  (+ .brain)"
fi

echo
echo "linked. start a session here:  claude    (or open the folder in Antigravity)"
