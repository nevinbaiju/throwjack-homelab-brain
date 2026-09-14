#!/usr/bin/env bash
# Step 1 of the migration spec: does an alarm WE wrote fire on the phone?
#
#   ./valarm-test.sh          alarm 2 minutes from now
#   ./valarm-test.sh 5        alarm 5 minutes from now
#   ./valarm-test.sh list     show what is stored
set -euo pipefail
cd "$(dirname "$0")"

HOST=${RADICALE_HOST:-127.0.0.1:5232}
USER="${CALDAV_USER:-admin}"
PASS=$(cut -d: -f2- users)
COL="http://$HOST/$USER/valarm-test"

if [ "${1:-}" = list ]; then
  curl -s -u "$USER:$PASS" -X PROPFIND -H 'Depth: 1' "$COL/" \
    | grep -oE '<[^>]*href>[^<]*' | sed 's/.*>/  /' || echo "  (nothing)"
  exit 0
fi

MINS=${1:-2}

# Create the VTODO collection if it is not there yet. Radicale needs the
# component set declared or iOS will not offer it under Reminders.
if ! curl -sf -u "$USER:$PASS" -o /dev/null "$COL/"; then
  curl -sf -u "$USER:$PASS" -X MKCOL "$COL/" -H 'Content-Type: application/xml' --data '<?xml version="1.0" encoding="UTF-8"?>
<create xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
 <set><prop>
  <resourcetype><collection/><C:calendar/></resourcetype>
  <C:supported-calendar-component-set><C:comp name="VTODO"/></C:supported-calendar-component-set>
  <displayname>VALARM test</displayname>
 </prop></set>
</create>' >/dev/null && echo "  created collection valarm-test" || { echo "MKCOL failed" >&2; exit 1; }
fi

UID_="valarm-$(date +%s)"
# Absolute UTC trigger, exactly as the brain would write it — not an offset,
# because an offset lets the phone recompute and slide past quiet hours.
FIRE_UTC=$(date -u -d "+${MINS} minutes" +%Y%m%dT%H%M%SZ)
FIRE_LOCAL=$(date -d "+${MINS} minutes" +%H:%M:%S)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DUE=$(date -d "+${MINS} minutes" +%Y%m%dT%H%M%S)
TZ_NAME=$(date +%Z)

read -r -d '' ICS <<EOF || true
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//focus-board-brain//valarm-test//EN
BEGIN:VTODO
UID:$UID_
DTSTAMP:$STAMP
CREATED:$STAMP
SUMMARY:VALARM test — if this buzzes, the spec holds
DESCRIPTION:Written by the server. No push service involved.
DUE;TZID=America/Los_Angeles:$DUE
PRIORITY:1
STATUS:NEEDS-ACTION
BEGIN:VALARM
UID:$UID_-alarm
ACTION:DISPLAY
TRIGGER;VALUE=DATE-TIME:$FIRE_UTC
DESCRIPTION:VALARM test — if this buzzes, the spec holds
END:VALARM
END:VTODO
END:VCALENDAR
EOF

printf '%s\n' "$ICS" | curl -sf -u "$USER:$PASS" -X PUT "$COL/$UID_.ics" \
  -H 'Content-Type: text/calendar; charset=utf-8' --data-binary @- \
  && echo "  wrote $UID_.ics"

# Read it back so we know the server kept the alarm, not just accepted it.
if curl -sf -u "$USER:$PASS" "$COL/$UID_.ics" | grep -q 'BEGIN:VALARM'; then
  echo "  VALARM survived the round trip on the server"
else
  echo "  !! server did not keep the VALARM" >&2; exit 1
fi

echo
echo "  alarm set for $FIRE_LOCAL $TZ_NAME  (${MINS}m from now)"
echo "  watch your phone and your Watch. Nothing else has to happen."
