"""The v1 -> v2 chore file migration, and mining last_done out of the old list."""
import sys, pathlib, tempfile, os
from datetime import date
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["BRAIN_ROOT"] = str(TMP)
import store, upkeep, caldav

fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else '  ' + detail}")
    if not ok: fails.append(label)

# The real v1 file, as it sits on the volume today.
V1 = """\
# Recurring chores. YOURS to edit — the brain writes these into the Upkeep
# list and then leaves them alone.
#
#   at:      HH:MM, 24-hour, local time
#   repeat:  daily | weekly | monthly | every N weeks | every N days

chores:
  - title: Have breakfast
    essential: true
    at: "11:00"
    repeat: daily
  - title: Load the dishwasher
    essential: true
    at: "23:30"
    repeat: daily
  - title: Go to sleep
    at: "00:00"
    repeat: daily
  - title: Laundry
    at: "12:00"
    repeat: weekly
    day: friday
  - title: Change the sheets
    at: "12:00"
    repeat: every 2 weeks
    day: friday
"""

path = TMP / "upkeep.yaml"
path.write_text(V1)
upkeep._cache = None
res = upkeep.migrate_yaml(path)
check("a v1 file is migrated", res["migrated"] is True, str(res))
check("every v1 chore is reported as dropped", len(res["dropped"]) == 5, str(res))
check("the drop list names them", "Laundry" in res["dropped"], str(res["dropped"]))

text = path.read_text()
check("the file declares version 2", "version: 2" in text)
check("the chore list starts empty", "chores: []" in text)
check("no v1 chore survives",
      not any(t in text for t in ("Laundry", "Change the sheets", "Go to sleep")))
# Only real YAML keys count. The header legitimately says "time_of_day:", which
# a naive substring check reads as the retired "day:".
live = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
import re as _re
retired = [ln for ln in live
           if _re.match(r"^\s*(-\s*)?(repeat|at|day|alarm)\s*:", ln)]
check("no retired key survives as a real key", not retired, str(retired))
check("the new header explains `every`", "every" in text and "last done" in text)
check("the header warns about `on` being a YAML boolean", "boolean" in text)

bak = TMP / "upkeep.yaml.bak"
check("the old file is kept as .bak", bak.exists())
check("...with its contents intact", bak.read_text() == V1)

# Loading it is clean, and migration does not repeat.
chores, errors = upkeep.load_with_errors(path)
check("the migrated file loads with no chores and no errors",
      chores == [] and errors == [], f"{chores} {errors}")
again = upkeep.migrate_yaml(path)
check("migration is idempotent", again["migrated"] is False, str(again))
check("...and does not touch the file again", path.read_text() == text)

# A file already at v2 is left entirely alone.
v2 = TMP / "already.yaml"
body = upkeep.V2_HEADER + "chores:\n- id: laundry\n  title: Laundry\n  every: 7d\n"
v2.write_text(body)
upkeep._cache = None
check("a v2 file is not migrated", upkeep.migrate_yaml(v2)["migrated"] is False)
check("...and is unchanged", v2.read_text() == body)
check("...and loads its chores", [c["slug"] for c in upkeep.load(v2)] == ["laundry"])

# --- save() keeps the header ---------------------------------------------
upkeep._cache = None
chores = upkeep.load(v2)
chores.append(upkeep._check({"id": "sheets", "title": "Change the sheets",
                             "every": "14d", "window": "weekend", "est_min": 15}, 0))
upkeep.save(chores, v2)
out = v2.read_text()
check("save keeps the header comments", "# Recurring chores" in out)
check("save keeps version: 2", "version: 2" in out)
check("save writes the new chore", "sheets" in out and "weekend" in out)
check("save writes explicit ids", out.count("id:") >= 2, out)
upkeep._cache = None
check("what save wrote loads back identically",
      [c["slug"] for c in upkeep.load(v2)] == ["laundry", "sheets"])
reloaded = upkeep.load(v2)[1]
check("...including the window", reloaded["days_set"] == upkeep.WEEKEND)
check("...and est_min", reloaded["est_min"] == 15)

upkeep.save([], v2)
upkeep._cache = None
check("save can empty the list again", upkeep.load(v2) == [])
check("...and still keeps the header", "# Recurring chores" in v2.read_text())

# --- mining last_done from the retired collection ------------------------
# iOS writes one VTODO per completion with a random uid. The brain never read
# them, so this is the only record of when anything was actually done. Three
# date forms appear in the wild and the old parser accepted only the first.
def vtodo(summary, completed=None, due=None):
    return {"summary": summary, "completed_at": completed, "due": due}

items = [
    vtodo("Laundry", "20260920T190000Z"),
    vtodo("Laundry", "20260927T183000Z"),          # later: this one wins
    vtodo("Laundry", "20260913T190000Z"),
    vtodo("Change the sheets", "20260928"),        # VALUE=DATE
    vtodo("Clean the floor", "20260901T120000"),   # floating / TZID
    vtodo("Clean the bathroom", None),             # never completed
    vtodo("Laundry", "20271231T120000Z"),          # in the future: ignored
    vtodo("", "20260920T190000Z"),                 # no title
]
today = date(2026, 9, 29)
mined = {}
for item in items:
    title = store.normalise_title(item["summary"])
    stamp = caldav.parse_dt(item["completed_at"] or "")
    if not title or stamp is None or stamp.date() > today:
        continue
    got = stamp.date().isoformat()
    if title not in mined or got > mined[title]:
        mined[title] = got

check("the latest completion per chore wins", mined.get("laundry") == "2026-09-27", str(mined))
check("a VALUE=DATE completion is read", mined.get("change the sheets") == "2026-09-28", str(mined))
check("a floating completion is read", mined.get("clean the floor") == "2026-09-01", str(mined))
check("a chore with no completion is absent", "clean the bathroom" not in mined)
check("a future completion is ignored", mined.get("laundry") != "2027-12-31")
check("an untitled item is skipped", "" not in mined)
check("titles are matched normalised, so case and spacing do not matter",
      store.normalise_title("  Change The Sheets ") == "change the sheets")

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'test_upkeep_migrate: all passed'}")
sys.exit(1 if fails else 0)
