"""The due engine. Pure functions, no database, no clock.

The headline case is the one the whole revamp exists for: a weekend-only chore
that comes due, is skipped, and must wait for the NEXT weekend rather than
nagging on Tuesday -- asserted day by day across six weeks, because an off-by-one
in next_eligible or missed_windows is invisible in a single-day test.
"""
import sys, pathlib
from datetime import date, timedelta
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import upkeep

fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + detail}")
    if not ok: fails.append(label)

def chore(**kw):
    kw.setdefault("title", "Change the sheets")
    kw.setdefault("every", "14d")
    return upkeep._check(kw, 0)

def st(c, *, last=None, seen=None, snooze=None, today):
    return upkeep.state_of(c, last_done=last, first_seen=seen,
                           snooze_until=snooze, today=today)

# --- every ----------------------------------------------------------------
check("7d parses", upkeep.parse_every("7d") == ("d", 7))
check("2w parses", upkeep.parse_every("2w") == ("w", 2))
check("1m parses", upkeep.parse_every("1m") == ("m", 1))
check("'14 days' parses", upkeep.parse_every("14 days") == ("d", 14))
check("2w == 14d", upkeep.add_every(date(2026, 9, 1), "2w")
                  == upkeep.add_every(date(2026, 9, 1), "14d"))
for bad in ("weekly", "0d", "", "7x", "d7", "-3d"):
    try:
        upkeep.parse_every(bad); check(f"{bad!r} rejected", False)
    except upkeep.ChoreError:
        check(f"{bad!r} rejected", True)

# Month arithmetic clamps instead of drifting into the next month.
check("Jan 31 + 1m -> Feb 28 (2026)",
      upkeep.add_every(date(2026, 1, 31), "1m") == date(2026, 2, 28))
check("Jan 31 + 1m -> Feb 29 (2028 leap)",
      upkeep.add_every(date(2028, 1, 31), "1m") == date(2028, 2, 29))
check("Dec 15 + 1m rolls the year",
      upkeep.add_every(date(2026, 12, 15), "1m") == date(2027, 1, 15))
check("Aug 31 + 1m -> Sep 30",
      upkeep.add_every(date(2026, 8, 31), "1m") == date(2026, 9, 30))
check("Nov 30 + 3m -> Feb 28",
      upkeep.add_every(date(2026, 11, 30), "3m") == date(2027, 2, 28))

# --- windows --------------------------------------------------------------
weekend = chore(window="weekend")
weekday = chore(window="weekday", title="Post the parcel", every="7d")
explicit = chore(days=["mon", "thu"], title="Bins", every="7d")
anyday = chore(title="Declutter workspace", every="1m")

check("weekend allows Sat", upkeep.window_allows(date(2026, 9, 19), weekend))
check("weekend allows Sun", upkeep.window_allows(date(2026, 9, 20), weekend))
check("weekend refuses Mon", not upkeep.window_allows(date(2026, 9, 21), weekend))
check("weekday refuses Sat", not upkeep.window_allows(date(2026, 9, 19), weekday))
check("days:[mon,thu] allows Thu", upkeep.window_allows(date(2026, 9, 24), explicit))
check("days:[mon,thu] refuses Wed", not upkeep.window_allows(date(2026, 9, 23), explicit))
check("no window allows any day",
      all(upkeep.window_allows(date(2026, 9, 21) + timedelta(days=i), anyday)
          for i in range(7)))

# `on:` is a YAML boolean, which is why the key is `days`.
try:
    upkeep._check({True: "friday", "title": "Laundry", "every": "7d"}, 0)
    check("boolean key (`on:`) is refused", False)
except upkeep.ChoreError as e:
    check("boolean key (`on:`) is refused", "`on:`" in str(e), str(e))
for bad, why in ((  {"days": []},            "empty days"),
                 (  {"window": "fortnight"}, "unknown window"),
                 (  {"days": ["funday"]},    "unknown weekday")):
    try:
        chore(**bad); check(f"{why} is refused", False)
    except upkeep.ChoreError:
        check(f"{why} is refused", True)
try:
    upkeep._check({"title": "X", "every": "7d", "evry": "8d"}, 0)
    check("unknown key is refused", False)
except upkeep.ChoreError as e:
    check("unknown key is refused", "evry" in str(e), str(e))
try:
    upkeep._check({"title": "X", "every": "7d", "at": "12:00"}, 0)
    check("retired key `at` is refused by name", False)
except upkeep.ChoreError as e:
    check("retired key `at` is refused by name", "retired" in str(e), str(e))

# An unsatisfiable window must RETURN (raising) inside the bound, never spin --
# the call site is /plan/draft, so a hang is a hung request.
impossible = dict(weekend, days_set=frozenset())
try:
    upkeep.next_eligible(date(2026, 9, 1), impossible, limit=30)
    check("unsatisfiable window raises within the bound", False)
except upkeep.ChoreError:
    check("unsatisfiable window raises within the bound", True)

# --- never done -----------------------------------------------------------
c = chore(title="Declutter workspace", every="1m")
s = st(c, seen=date(2026, 9, 29), today=date(2026, 9, 29))
check("a chore you just added is due today, not in a cadence",
      (s.state, s.overdue_days) == (upkeep.DUE, 0), str(s))
s = st(c, seen=date(2026, 9, 20), today=date(2026, 9, 29))
check("never-done anchors on first_seen", s.state == upkeep.DUE and s.overdue_days == 9, str(s))

# A never-done weekend chore added on a Tuesday waits for Saturday.
s = st(chore(window="weekend"), seen=date(2026, 9, 29), today=date(2026, 9, 29))
check("never-done weekend chore added midweek is not due yet",
      (s.state, s.next_day) == (upkeep.NOT_DUE, date(2026, 10, 3)), str(s))

# --- plain cadence, no window --------------------------------------------
c = chore(title="Water plants", every="7d")
last = date(2026, 9, 22)
check("T-1 not due", st(c, last=last, today=date(2026, 9, 28)).state == upkeep.NOT_DUE)
s = st(c, last=last, today=date(2026, 9, 29))
check("T exactly due", (s.state, s.overdue_days) == (upkeep.DUE, 0), str(s))
s = st(c, last=last, today=date(2026, 10, 2))
check("T+3 still due, overdue climbing",
      (s.state, s.overdue_days, s.missed_windows) == (upkeep.DUE, 3, 0), str(s))
check("no window means it never goes to waiting",
      all(st(c, last=last, today=date(2026, 9, 29) + timedelta(days=i)).state == upkeep.DUE
          for i in range(30)))

# Doing it late re-anchors from when you did it, not from when it was due.
# This is the entire point: iOS advanced from the previous DUE date instead.
s = st(c, last=date(2026, 9, 29), today=date(2026, 10, 5))
check("late completion re-anchors the cadence",
      (s.state, s.next_day) == (upkeep.NOT_DUE, date(2026, 10, 6)), str(s))

# --- THE HEADLINE: weekend chore, six weeks, day by day ------------------
# every 14d, window weekend, last done Saturday 2026-09-05.
#   base = 09-19 (Sat) -> eligible 09-19
#   due 09-19/20, waiting Mon-Fri, due again 09-26/27, and so on.
sheets = chore(window="weekend", every="14d")
seq = {}
for i in range(0, 57):
    d = date(2026, 9, 6) + timedelta(days=i)
    seq[d] = st(sheets, last=date(2026, 9, 5), today=d)

def expect(d, state, next_day, overdue, missed):
    got = seq[date.fromisoformat(d)]
    want = (state, date.fromisoformat(next_day), overdue, missed)
    have = (got.state, got.next_day, got.overdue_days, got.missed_windows)
    check(f"{d} {date.fromisoformat(d):%a} -> {state}", have == want,
          f"want {want}, got {have}")

expect("2026-09-06", upkeep.NOT_DUE, "2026-09-19", 0, 0)
expect("2026-09-18", upkeep.NOT_DUE, "2026-09-19", 0, 0)
expect("2026-09-19", upkeep.DUE,     "2026-09-19", 0, 0)   # Sat: the window opens
expect("2026-09-20", upkeep.DUE,     "2026-09-20", 1, 0)   # Sun
expect("2026-09-21", upkeep.WAITING, "2026-09-26", 2, 1)   # Mon: missed one weekend
expect("2026-09-25", upkeep.WAITING, "2026-09-26", 6, 1)   # Fri
expect("2026-09-26", upkeep.DUE,     "2026-09-26", 7, 1)   # Sat: back, 1 deferral
expect("2026-09-27", upkeep.DUE,     "2026-09-27", 8, 1)   # Sun
expect("2026-09-28", upkeep.WAITING, "2026-10-03", 9, 2)   # Mon: two now
expect("2026-10-03", upkeep.DUE,     "2026-10-03", 14, 2)  # Sat
expect("2026-10-05", upkeep.WAITING, "2026-10-10", 16, 3)  # Mon
expect("2026-10-10", upkeep.DUE,     "2026-10-10", 21, 3)  # Sat
expect("2026-10-31", upkeep.DUE,     "2026-10-31", 42, 6)  # six weekends deferred

check("weekend chore is never DUE on a weekday",
      not any(s.state == upkeep.DUE and d.weekday() < 5 for d, s in seq.items()))
check("overdue only ever grows",
      all(seq[d].overdue_days <= seq[d + timedelta(days=1)].overdue_days
          for d in sorted(seq)[:-1] if seq[d].state != upkeep.NOT_DUE))

# --- weekday-only chore missed on Friday ---------------------------------
post = chore(window="weekday", title="Post the parcel", every="7d")
s = st(post, last=date(2026, 9, 18), today=date(2026, 9, 25))   # Fri, due
check("weekday chore due on Friday", s.state == upkeep.DUE, str(s))
for d, lbl in ((date(2026, 9, 26), "Sat"), (date(2026, 9, 27), "Sun")):
    s = st(post, last=date(2026, 9, 18), today=d)
    check(f"weekday chore waits over {lbl}, resumes Mon",
          (s.state, s.next_day) == (upkeep.WAITING, date(2026, 9, 28)), str(s))

# --- snooze ---------------------------------------------------------------
c = chore(title="Declutter workspace", every="1m")
s = st(c, last=date(2026, 8, 29), snooze=date(2026, 10, 2), today=date(2026, 9, 30))
check("snooze suppresses a due chore",
      (s.state, s.next_day) == (upkeep.SNOOZED, date(2026, 10, 3)), str(s))
s = st(c, last=date(2026, 8, 29), snooze=date(2026, 10, 2), today=date(2026, 10, 3))
check("snooze expires", s.state == upkeep.DUE, str(s))
check("snooze keeps the overdue count honest",
      st(c, last=date(2026, 8, 29), snooze=date(2026, 10, 2),
         today=date(2026, 9, 30)).overdue_days == 1)

# --- slots ---------------------------------------------------------------
check("19:00 is a slot", upkeep.validate_slot("19:00") == "19:00")
check(" 09:05 trims", upkeep.validate_slot(" 09:05 ") == "09:05")
for bad in ("7pm", "25:00", "19:0", "19:60", "", "2:30", None, "02:30", "05:59"):
    try:
        upkeep.validate_slot(bad); check(f"slot {bad!r} rejected", False)
    except upkeep.ChoreError:
        check(f"slot {bad!r} rejected", True)
check("06:00 is the earliest slot", upkeep.validate_slot("06:00") == "06:00")
check("23:59 is the latest slot", upkeep.validate_slot("23:59") == "23:59")

# --- source keys ---------------------------------------------------------
check("source_key round-trips",
      upkeep.parse_source_key(upkeep.source_key("laundry", date(2026, 9, 29)))
      == ("laundry", date(2026, 9, 29)))
for bad in (None, "", "t_abc", "upkeep:laundry", "upkeep:laundry:not-a-date",
            "chore:u_laundry", "upkeep:a:b:c"):
    check(f"{bad!r} is not an instance key", upkeep.parse_source_key(bad) is None)

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'test_upkeep_due: all passed'}")
sys.exit(1 if fails else 0)
