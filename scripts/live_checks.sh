#!/usr/bin/env bash
# T17 checks against the live URL. Free: no chat messages, so no model calls.
# usage: STAFF_PASSWORD=... scripts/live_checks.sh https://<app>.up.railway.app
set -euo pipefail
url=${1:?usage: live_checks.sh <base-url>}
fail=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi; }

h=$(curl -sS -D - -o /dev/null "$url/")  # GET: the page has no HEAD route
check "page answers 200" "grep -q '^HTTP/[0-9.]* 200' <<<\"\$h\""
check "HSTS header" "grep -qi '^strict-transport-security:' <<<\"\$h\""
check "CSP header" "grep -qi '^content-security-policy:' <<<\"\$h\""
# Railway's edge adds "server: railway-hikari"; only a header from our app (uvicorn) is a leak
check "no app server header" "! grep -qi '^server:.*uvicorn' <<<\"\$h\""
check "/health is ok" "curl -sS \"$url/health\" | grep -q '\"ok\"'"
for p in docs redoc openapi.json; do
  check "/$p is off" "[ \"\$(curl -s -o /dev/null -w '%{http_code}' \"$url/$p\")\" = 404 ]"
done
if [ -n "${STAFF_PASSWORD:-}" ]; then
  real=$(curl -sS -u "staff:$STAFF_PASSWORD" "$url/staff/client-ip")
  fake=$(curl -sS -u "staff:$STAFF_PASSWORD" -H 'X-Forwarded-For: 9.9.9.9' "$url/staff/client-ip")
  echo "     client ip without / with a fake X-Forwarded-For: $real / $fake"
  check "a fake X-Forwarded-For is ignored" "[ \"\$real\" = \"\$fake\" ] && ! grep -q 9.9.9.9 <<<\"\$fake\""
else
  echo "skip forwarded-IP check (set STAFF_PASSWORD)"
fi
exit $fail
