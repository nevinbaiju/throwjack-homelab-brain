#!/usr/bin/env bash
# Small wrapper so nothing needs a multi-line paste.
#   ./brain.sh health
#   ./brain.sh capture "p2: try the brain out"
#   ./brain.sh triage
#   ./brain.sh board
#   ./brain.sh logs
set -euo pipefail
cd "$(dirname "$0")"
TOKEN=$(grep -E '^BRAIN_TOKEN=' .env | cut -d= -f2-)
URL=http://localhost:5010
AUTH="Authorization: Bearer $TOKEN"

case "${1:-board}" in
  health)  curl -s "$URL/health"; echo ;;
  capture)
    shift
    [ $# -gt 0 ] || { echo "usage: ./brain.sh capture \"some text\"" >&2; exit 2; }
    curl -s -X POST "$URL/capture" -H "$AUTH" -H 'Content-Type: text/plain' \
         -H 'X-Source: cli' --data "$*"; echo ;;
  triage)  curl -s -X POST "$URL/triage" -H "$AUTH"; echo ;;
  upkeep)  curl -s -X POST "$URL/upkeep/sync" -H "$AUTH"; echo ;;
  dump)
    # ./brain.sh dump doordash notes.md    (or: ... dump doordash  then type, ^D)
    shift
    slug="${1:?usage: ./brain.sh dump <project> [file]}"; shift || true
    if [ $# -gt 0 ] && [ -f "$1" ]; then src="$1"; else src=/dev/stdin; echo "(reading dump from stdin — finish with Ctrl-D)" >&2; fi
    curl -s -X POST "$URL/project/$slug/dump" -H "$AUTH" \
         -H 'Content-Type: text/plain' --data-binary @"$src"; echo ;;
  project) shift; curl -s "$URL/project/${1:?usage: ./brain.sh project <slug>}" -H "$AUTH" ;;
  sync)
    # Manual only, by design. Pulls captures/ from one project, or all of them.
    shift
    if [ $# -gt 0 ]; then
      curl -s -X POST "$URL/project/$1/sync" -H "$AUTH"; echo
    else
      curl -s -X POST "$URL/sync" -H "$AUTH"; echo
    fi ;;
  board)   curl -s "$URL/board" -H "$AUTH" ;;
  upcoming) curl -s "$URL/upcoming" -H "$AUTH" ;;
  ui)
    lan=$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' | head -1)
    ts=$(tailscale ip -4 2>/dev/null | head -1)
    [ -n "$lan" ] && echo "  LAN      http://$lan:5010/"
    [ -n "$ts" ]  && echo "  tailnet  http://$ts:5010/"
    echo
    if grep -q '^BRAIN_ADMIN_PASSWORD_HASH=.' .env 2>/dev/null; then
      echo "  Sign in as: $(grep '^BRAIN_ADMIN_USER=' .env | cut -d= -f2-)"
    else
      echo "  No admin user yet — run: ./brain.sh setpass"
    fi ;;
  setpass)
    # Sets the browser login. The password is scrypt-hashed before it touches
    # disk; the plaintext never leaves this shell.
    user="${2:-$USER}"
    printf 'Username [%s]: ' "$user" >&2; read -r entered; [ -n "$entered" ] && user="$entered"
    printf 'Password: ' >&2; stty -echo 2>/dev/null; read -r p1; stty echo 2>/dev/null; echo >&2
    printf 'Again:    ' >&2; stty -echo 2>/dev/null; read -r p2; stty echo 2>/dev/null; echo >&2
    [ -n "$p1" ] || { echo "empty password, aborted" >&2; exit 1; }
    [ "$p1" = "$p2" ] || { echo "passwords do not match" >&2; exit 1; }
    # Hash on the HOST: auth.py is pure stdlib, so this works whether or not
    # the container is running or up to date. Depending on the container made
    # setpass fail exactly when you most needed it — locked out, image stale.
    hash=$(printf '%s' "$p1" | PYTHONPATH="$PWD" python3 -c \
      'import sys,auth; print(auth.hash_password(sys.stdin.read()))' 2>/dev/null) || hash=""
    if [ -z "$hash" ]; then
      hash=$(printf '%s' "$p1" | podman exec -i brain python3 -c \
        'import sys,auth; print(auth.hash_password(sys.stdin.read()))' 2>/dev/null) || hash=""
    fi
    [ -n "$hash" ] || { echo "could not hash the password (no working python3?)" >&2; exit 1; }
    case "$hash" in
      scrypt:*:*) : ;;
      *) echo "hash came back in an unexpected format — stale auth.py?" >&2; exit 1 ;;
    esac
    cp .env ".env.bak.$(date +%s)"
    grep -v '^BRAIN_ADMIN_USER=\|^BRAIN_ADMIN_PASSWORD_HASH=' .env > .env.tmp
    printf 'BRAIN_ADMIN_USER=%s\nBRAIN_ADMIN_PASSWORD_HASH=%s\n' "$user" "$hash" >> .env.tmp
    mv .env.tmp .env; chmod 600 .env
    echo "  set for '$user'."
    echo "  Now load it:  ./brain.sh rebuild" ;;
  inbox)   curl -s "$URL/inbox/today" -H "$AUTH" ;;
  logs)    podman logs --tail "${2:-40}" "${3:-brain}" ;;
  syncpass)
    # Reset the Syncthing GUI password deterministically. Reads the password
    # from stdin (--gui-password=-) so it never lands in shell history.
    CFG="${SYNCTHING_CONFIG:-$HOME/.local/share/syncthing}"
    user="${2:-$USER}"
    cp "$CFG/config.xml" "$CFG/config.xml.bak.$(date +%s)" 2>/dev/null && echo "backed up config.xml"
    printf 'New GUI password for %s: ' "$user" >&2
    stty -echo 2>/dev/null; read -r pw; stty echo 2>/dev/null; echo >&2
    [ -n "$pw" ] || { echo "empty password, aborted" >&2; exit 1; }
    printf '%s' "$pw" | podman exec -i syncthing \
        syncthing generate --home=/var/syncthing/config \
                           --gui-user="$user" --gui-password=- || exit 1
    podman restart syncthing >/dev/null
    echo "restarted. verifying..."
    for i in $(seq 1 20); do
      code=$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:8384/rest/system/status || true)
      [ "$code" = "403" ] && { echo "  API returns 403 unauthenticated — auth is on"; break; }
      sleep 1
    done
    # Syncthing 2.x with sendBasicAuthPrompt=false REJECTS HTTP Basic auth.
    # The browser logs in against this session endpoint, so that is what to test.
    code=$(printf '{"username":"%s","password":"%s"}' "$user" "$pw" \
      | curl -sk -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
             --data-binary @- https://127.0.0.1:8384/rest/noauth/auth/password || true)
    case "$code" in
      200|204) echo "  login with these credentials WORKS (session auth $code)" ;;
      403)     echo "  !! credentials rejected (403) — password did not take" ;;
      *)       echo "  !! unexpected response $code" ;;
    esac
    ;;
  doctor)
    # End-to-end check of the laptop -> board chain.
    CTX="${BRAIN_DATA:-/var/mnt/storage/brain}/contexts"
    SCFG="${SYNCTHING_CONFIG:-$HOME/.local/share/syncthing}/config.xml"
    echo "containers"
    for c in brain litellm syncthing; do
      printf '  %-10s %s\n' "$c" "$(podman inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
    done
    echo
    echo "syncthing"
    if [ -f "$SCFG" ]; then
      K=$(sed -n 's/.*<apikey>\(.*\)<\/apikey>.*/\1/p' "$SCFG" | head -1)
      curl -sk -H "X-API-Key: $K" "https://127.0.0.1:8384/rest/db/status?folder=brain-contexts" \
        | python3 -c "import json,sys;d=json.load(sys.stdin);print(f\"  folder: {d.get('state')}  files={d.get('localFiles')}  pending_in={d.get('needFiles')}  errors={d.get('errors')}\")" 2>/dev/null \
        || echo "  folder: could not query"
      curl -sk -H "X-API-Key: $K" https://127.0.0.1:8384/rest/system/connections \
        | python3 -c "
import json,sys
d=json.load(sys.stdin).get('connections',{})
live=[(k,v) for k,v in d.items() if v.get('connected')]
print(f'  peers connected: {len(live)}')
for k,v in live: print(f\"    {k[:11]}...  {v.get('address')}  {v.get('type')}\")
if not live: print('    (none — laptop offline or not paired)')" 2>/dev/null
    else echo "  no syncthing config found"; fi
    echo
    echo "captures waiting to be ingested"
    found=0
    for d in "$CTX"/*/; do
      [ -d "$d/captures" ] || continue
      n=$(find "$d/captures" -maxdepth 1 -name '*.md' -o -maxdepth 1 -name '*.txt' 2>/dev/null | wc -l)
      [ "$n" -gt 0 ] && { printf '  %-12s %s file(s)\n' "$(basename "$d")" "$n"; found=1; }
    done
    [ "$found" = 0 ] && echo "  (none — run a sync, or nothing new from the laptop)"
    echo
    echo "brain"
    curl -s "$URL/health" -H "$AUTH" | sed 's/^/  /'; echo
    curl -s "$URL/board" -H "$AUTH" | tail -3 | sed 's/^/  /'
    ;;
  status)
    for c in brain litellm syncthing; do
      st=$(podman inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
      printf '  %-10s %s\n' "$c" "$st"
    done
    printf '  %-10s ' "gui :8384"
    curl -sf -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8384/ 2>/dev/null || echo "not listening"
    ;;
  reset)
    # ./brain.sh reset        board + database. inbox/ is kept, so the next
    #                         triage rebuilds the same tasks from your captures.
    # ./brain.sh reset all    clean slate: also archives the capture files and
    #                         project context folders so nothing comes back.
    # Nothing is ever deleted — archived files move under archive/.
    BRAIN=/var/mnt/storage/brain
    if [ "${2:-}" = all ]; then
      read -rp "Clean slate: clear board + DB, archive captures and project contexts? [y/N] " a
    else
      read -rp "Clear board + DB? (captures kept, so tasks will rebuild) [y/N] " a
    fi
    [ "$a" = y ] || { echo aborted; exit 0; }
    curl -s -X POST "$URL/reset" -H "$AUTH"; echo
    if [ "${2:-}" = all ]; then
      ts=$(date +%Y%m%d-%H%M%S)
      mkdir -p "$BRAIN/archive/inbox-$ts" "$BRAIN/archive/contexts-$ts"
      n=$(find "$BRAIN/inbox" -maxdepth 1 -name '*.jsonl' 2>/dev/null | wc -l)
      [ "$n" -gt 0 ] && mv "$BRAIN"/inbox/*.jsonl "$BRAIN/archive/inbox-$ts/"
      echo "  archived $n capture file(s)"
      m=0
      for d in "$BRAIN"/contexts/*/; do
        b=$(basename "$d")
        case "$b" in .*) continue;; esac
        [ -d "$d" ] && mv "$d" "$BRAIN/archive/contexts-$ts/" && m=$((m+1))
      done
      echo "  archived $m project context(s)"
    fi ;;
  rebuild)
    podman compose up -d --build
    echo "waiting for the service..."
    for i in $(seq 1 30); do
      if curl -sf "$URL/health" >/dev/null 2>&1; then
        echo "up. endpoints:"
        for ep in /health /board; do
          printf '  %-8s %s\n' "$ep" "$(curl -s -o /dev/null -w '%{http_code}' "$URL$ep" -H "$AUTH")"
        done
        echo "(/board should be 200, not 404)"
        exit 0
      fi
      sleep 1
    done
    echo "did not come up in 30s — check: ./brain.sh logs" >&2; exit 1 ;;
  *) echo "usage: ./brain.sh {rebuild|health|capture <text>|dump <proj> [file]|project <proj>|sync <proj>|upkeep|triage|board|upcoming|ui|setpass [user]|inbox|logs [container]|status|doctor|syncpass [user]|reset [all]}" >&2; exit 2 ;;
esac
