# Tests

Scratch only. Per AGENTS.md: **a test must never touch the real board or the
real database.** The endpoint tests assert their `BRAIN_ROOT` is under `/tmp`
and refuse to run otherwise; the unit tests build their own sqlite file in a
temp dir and stub `triage.project_to_board` / `board.remove_task` so nothing
can reach CalDAV.

    tests/run            everything that runs on the host
    tests/run-in-brain   the endpoint tests, inside the container

These exist because the same bugs kept coming back: behaviour verified once by
hand, then broken by the next change with nothing to catch it. Every check here
corresponds to something that actually went wrong.
