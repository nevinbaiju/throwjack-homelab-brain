#!/usr/bin/env bash
# Check the CURRENT *Syncthing GUI* password without changing it.
# (Not the brain's login -- that is `./brain.sh setpass`.)
printf 'password for %s: ' "${1:-$USER}" >&2
stty -echo; read -r pw; stty echo; echo >&2
code=$(printf '{"username":"%s","password":"%s"}' "${1:-$USER}" "$pw" \
  | curl -sk -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
         --data-binary @- https://127.0.0.1:8384/rest/noauth/auth/password)
case "$code" in
  200|204) echo "OK — these credentials work ($code). The browser will accept them." ;;
  403)     echo "REJECTED (403) — wrong password." ;;
  *)       echo "unexpected: $code" ;;
esac
