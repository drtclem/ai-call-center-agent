"""T18 scheduled alerts (D15.6). Exit 1 fails the GitLab pipeline, which emails the owner.

    python scripts/alert_check.py health https://<app>.up.railway.app     # hourly, free
    python scripts/alert_check.py conversations https://<app>...          # daily, ~5¢ of model use

The conversations are one KB question and one slot check for a canary customer (no booking is
made, so the demo's slots aren't used up). Stdlib only, so the CI job needs no install.
"""

import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# (what to send, words any of which must be in the reply)
KB: list[tuple[str, list[str]]] = [("How much is your diagnostic visit?", ["$89"])]
SLOTS: list[
    tuple[str, list[str]]
] = [  # the canary customer Wren Halvorsen (D20.8): fictional, safe to verify as
    ("Hi, I'm Wren Halvorsen, phone 512-555-0191. My AC is blowing warm air.", []),
    ("I already tried the thermostat and filter. What repair times do you have?", ["1."]),
]


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def health(base: str) -> list[str]:
    status, body = _get(base + "/health?deep=1")
    return [] if status == 200 else [f"/health?deep=1 returned {status}: {body[:200]}"]


def conversation(base: str, turns: list[tuple[str, list[str]]]) -> list[str]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    host = urllib.parse.urlsplit(base)
    problems: list[str] = []
    for text, want in turns:
        req = urllib.request.Request(
            base + "/chat",
            data=urllib.parse.urlencode({"message": text}).encode(),
            headers={"HX-Request": "true", "Origin": f"{host.scheme}://{host.netloc}"},
        )
        with opener.open(req, timeout=90) as r:
            reply = r.read().decode()
        if 'aria-label="Handoff ticket"' in reply:  # the ticket card (_messages.html)
            problems.append(f"handed off after {text!r}")
            break
        if want and not any(w in reply for w in want):
            problems.append(f"reply to {text!r} lacks any of {want}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("health", "conversations"):
        print(__doc__)
        return 2
    base = argv[2].rstrip("/")
    problems = (
        health(base) if argv[1] == "health" else conversation(base, KB) + conversation(base, SLOTS)
    )
    print(json.dumps({"check": argv[1], "problems": problems}))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
