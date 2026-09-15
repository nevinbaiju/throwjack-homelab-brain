# Working on one of Nevin's projects

This folder is shared state for the Focus Board Brain. If there is a folder
here matching the project you are working on, use it.

## At the start of a session

Read `<project>/STATE.md` for where things stand, and `<project>/tasks.json`
for the current board slice. `<project>/BRIEF.md` holds Nevin's own notes and
constraints — change it only if he asks you to.

## Start by asking him about the project

Do this once, near the start of a session, before doing the work.

The plan was written by a small free-tier model from whatever he happened to
dump at the time. It cannot see the code or the repo, and knows nothing he did
not think to type. You can. So titles are often vaguer than they should be,
the ordering often does not reflect what unblocks what, and some tasks are
scoped wrong.

Ask the questions that would change the plan — two or three real ones, not a
questionnaire. Constraints, what he has already decided, what "done" looks
like, what he is actually worried about.

Then write what you learned as a capture whose FIRST line is `replan:`

    <project>/captures/2026-09-14-what-i-learned.md
    ---
    replan:
    <everything you now understand that the original plan did not>

The brain picks that up on its own timer and re-plans against the tasks that
already exist: it can retitle a vague task, reorder the sequence, rescope an
estimate, or drop work the new context made irrelevant. Nevin does not have to
run anything.

Every capture, promotion, retitle and drop is appended to `<project>/LOG.md`,
so you can see what the brain did with what you wrote.

You cannot edit tasks directly, and should not try. Improve the context and
the tasks follow.

## When something becomes actionable

Write a new markdown file into `<project>/captures/`. One item per file, plain
prose is fine — a sentence is enough. For example:

    captures/2026-09-12-nyquist.md
    ---
    Problem set 4 needs the Nyquist plots redone before Thursday.

The brain ingests these on a timer, turns them into tasks, and moves the file
to `captures/.ingested/`. You do not need an API key, a network call, or an
MCP server — just write the file.

## To close or move a task you worked on

Write a capture whose FIRST line is a directive, using the task id from
`<project>/tasks.json`:

    done: t_a3d66700d70d
    doing: t_334ca79ea71e
    blocked: t_ef1e0428e710
    drop: t_1234567890ab

`done` closes it, `doing` promotes it to In Progress, `blocked` moves it to
Waiting, `drop` archives it. Anything after the first line is kept as a note.
The id must match exactly; an unknown id is treated as an ordinary new capture
rather than guessed at.

Without this, nothing you do can close a card — captures only ever create.

His task lists live in Apple Reminders (Backlog, In Progress, Done, Upkeep),
synced from his server. Each step is its own checkable item. `tasks.json` is
where you look up task ids — it is the reliable source, not the phone.

## Do not

- Edit `STATE.md` or `tasks.json`. The brain owns both and will overwrite you.
- Delete anything in `dumps/`. Those are raw thinking, kept verbatim.
- Assume a task exists because you wrote a capture. Check `tasks.json` next
  session to see how it was filed.
