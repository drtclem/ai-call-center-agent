"""Discovery report (D2, T3): final labels, volume ranking and labeler agreement.

Run: uv run python -m discovery.report   (offline: reads the committed caches)
Writes discovery/labels.csv and discovery/report.md.

Owner overrides (only needed when the labelers disagree): discovery/owner_overrides.json,
a JSON object mapping call_id -> bucket id, e.g. {"CALL-20260709-0315": "warranty"}.
"""

import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from discovery.common import CACHE, DATA, INDEX, call_ids, load_cache, transcript
from discovery.label import JEV_CACHE, OPUS_CACHE, SUGGEST_CACHE, cache_version, load_buckets
from discovery.pass1 import PROMPT_VERSION as PASS1_VERSION

LABELS_CSV = Path("discovery/labels.csv")
OVERRIDES = Path("discovery/owner_overrides.json")  # call_id -> bucket, from the owner review
REPORT = Path("discovery/report.md")
DB = DATA / "scheduling_db/meridian_scheduling.sqlite"
PLUMBING_RE = r"sink|drain|water heater|pilot|toilet|leak"
NORTH = {"Round Rock", "Cedar Park", "Pflugerville", "Georgetown", "Leander", "Hutto"}

# The brief asks for the top 4–5 call drivers. The 10 labeling buckets roll up into drivers by
# what the caller needs done (owner, 2026-10-19). Labels are unchanged; this is reporting only.
DRIVERS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("Appointments: book, change, confirm", ("repair_service_booking", "existing_appointment_mgmt",
     "maintenance_tuneup_booking", "replacement_estimate"), "Handled (scheduling flow + RAG)"),
    ("Membership", ("membership",), "Handled (RAG + account lookup)"),
    ("Billing", ("billing_payments",), "Handled (RAG + account lookup)"),
    ("Warranty", ("warranty",), "Handled (RAG + warranty-visit booking)"),
    ("Complaints and disputes", ("complaints_disputes",), "Escalated"),
    ("Safety emergency", ("safety_emergency",), "Escalated (safety script first)"),
    ("Not a service call", ("non_customer_other",), "Short answer or referral"),
)  # fmt: skip


def cohen_kappa(a: list[str], b: list[str]) -> float:
    """Agreement corrected for chance. 1.0 = perfect; 0 = chance level."""
    n = len(a)
    if n == 0 or n != len(b):
        raise ValueError("need two equal-length, non-empty label lists")
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def caller_city(text: str, con: sqlite3.Connection) -> str | None:
    """City of the caller when the transcript identifies them.

    Tries account number, then phone, then a "<street>, <City>" address in a customer line.
    On this dataset only the address path ever matches: the transcripts' account numbers and
    phones are not in the scheduling DB (I18).
    """
    if m := re.search(r"MHC-\d{5}", text):
        row = con.execute(
            "SELECT city FROM customers WHERE customer_id=?", (m.group(0),)
        ).fetchone()
        if row:
            return str(row[0])
    if m := re.search(r"\b\d{3}-\d{3}-\d{4}\b", text):
        row = con.execute("SELECT city FROM customers WHERE phone=?", (m.group(0),)).fetchone()
        if row:
            return str(row[0])
    cities = [c for (c,) in con.execute("SELECT DISTINCT city FROM customers")]
    for line in text.splitlines():
        if line.startswith("Customer:"):
            for c in cities:
                if re.search(rf",\s*{re.escape(str(c))}\b", line):
                    return str(c)
    return None


def build() -> tuple[list[dict[str, str]], str]:
    opus = load_cache(OPUS_CACHE, cache_version())
    jev = load_cache(JEV_CACHE, cache_version())
    suggest = load_cache(SUGGEST_CACHE, cache_version())
    pass1 = load_cache(CACHE / "pass1.jsonl", PASS1_VERSION)
    overrides: dict[str, str] = json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}
    ids = call_ids()
    missing = [c for c in ids if c not in opus or c not in jev]
    if missing:
        raise SystemExit(f"labels missing for {len(missing)} calls")
    with INDEX.open() as f:
        seconds = {r["call_id"]: int(r["duration_sec"]) for r in csv.DictReader(f)}

    rows: list[dict[str, str]] = []
    for c in ids:
        a, b = opus[c]["primary"], jev[c]["primary"]
        if a == b:
            final, by = a, "labelers_agree"
        elif c in overrides:
            final, by = overrides[c], "owner"
        else:
            raise SystemExit(f"{c}: labelers disagree and the owner hasn't decided")
        rows.append(
            {
                "call_id": c,
                "opus": a,
                "jev": b,
                "suggested": suggest.get(c, {}).get("pick", ""),
                "final": final,
                "decided_by": by,
                "secondary": opus[c]["secondary"] or "",
                "duration_sec": str(seconds[c]),
                "pass1_outcome": pass1[c]["outcome"],
            }
        )

    names = {str(b["id"]): str(b["name"]) for b in load_buckets()}
    total = len(rows)
    vol = Counter(r["final"] for r in rows)
    mins = Counter[str]()
    for r in rows:
        mins[r["final"]] += int(r["duration_sec"])
    sec = Counter(r["secondary"] for r in rows if r["secondary"])
    outcomes: dict[str, Counter[str]] = {}
    for r in rows:
        outcomes.setdefault(r["final"], Counter())[r["pass1_outcome"]] += 1
    agree = sum(r["opus"] == r["jev"] for r in rows)
    kappa = cohen_kappa([r["opus"] for r in rows], [r["jev"] for r in rows])
    by_owner = sum(r["decided_by"] == "owner" for r in rows)
    low = sorted(jev.values(), key=lambda r: float(r["confidence"]))[:6]
    low_conf_note = (
        "Jev's six least-confident calls ("
        f"{float(low[0]['confidence']):.2f}–{float(low[-1]['confidence']):.2f}) are: "
        + ", ".join(f"{n}× `{b}`" for b, n in Counter(r["primary"] for r in low).most_common())
        + "."
    )
    confusion = Counter((r["opus"], r["jev"]) for r in rows if r["opus"] != r["jev"])
    covered = [b for _, bs, _ in DRIVERS for b in bs]
    # every real bucket in exactly one driver; any `other` label would also fail here
    if (
        sorted(covered) != sorted(set(covered))
        or set(covered) != set(names) - {"other"}
        or vol["other"]
    ):
        raise SystemExit("every labeled bucket must belong to exactly one driver")
    drivers = sorted(
        ((name, sum(vol[b] for b in bs), bs, agent) for name, bs, agent in DRIVERS),
        key=lambda d: -d[1],
    )
    top5 = sum(n for _, n, _, _ in drivers[:5])
    driver_rows = [
        f"| {i} | **{name}** | {n} | {n / total:.1%} | "
        + ", ".join(f"`{b}` {vol[b]}" for b in bs)
        + f" | {agent} |"
        for i, (name, n, bs, agent) in enumerate(drivers, 1)
    ]

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    plumbing = plumbing_known = plumbing_north = 0
    estimates = estimates_known = estimates_austin = 0
    for r in rows:
        text = transcript(r["call_id"])
        is_plumbing = r["final"] == "repair_service_booking" and re.search(PLUMBING_RE, text, re.I)
        if not (is_plumbing or r["final"] == "replacement_estimate"):
            continue
        city = caller_city(text, con)
        if is_plumbing:
            plumbing += 1
            plumbing_known += city is not None
            plumbing_north += city in NORTH
        else:
            estimates += 1
            estimates_known += city is not None
            estimates_austin += city == "Austin"
    con.close()

    out = [
        "# Discovery report: why customers call Meridian",
        "",
        f"_{total} calls, July–Sept 2026. Method (D2): open-ended pass-1 summaries (Haiku) → "
        "bucket list from the data (Opus, owner-signed 2026-10-19, `discovery/buckets.md`) → two "
        "blind labelers from different model families: Opus (reads the transcript) and Jev "
        "`jev-1.14` via OpenRouter (fixed-choice classifier). Regenerate offline with "
        "`uv run python -m discovery.report`._",
        "",
        "## Labeler agreement",
        "",
        f"- Primary bucket: **{agree}/{total} ({agree / total:.0%})**, "
        f"Cohen's kappa **{kappa:.3f}**.",
        f"- Disagreements for owner review: **{total - agree}**; decided by owner: {by_owner}.",
        f"- `other` (fits no bucket): **{vol.get('other', 0)}** calls.",
        "- **How to read this:** the transcripts are templated and each call has one clear "
        "purpose, so perfect agreement mainly says the buckets are unambiguous on this data, "
        "not that labels on messier real-world calls would be this clean. Claude also "
        "spot-checked 20 random calls against their transcripts: 20/20 matched "
        "(`discovery/spotcheck.md`). " + low_conf_note,
        "",
        "## Call drivers (ranked)",
        "",
        "The top 5 drivers cover "
        + f"**{top5} of {total} calls ({top5 / total:.0%})**. Rows 6–7 are below the top 5 but "
        'listed for completeness (safety calls are always escalated). "Handled" means the agent '
        "finishes most calls in that driver; specific requests inside it that need tools Meridian "
        "hasn't provided still hand off (D3). Driver 1 includes 9 late-technician calls "
        '("my window passed, will I be charged?"): the KB says a missed window is "never '
        'charged as a no-show" and the caller can reschedule at no charge; any other fee or refund '
        "request (e.g. waiving the diagnostic fee) hands off.",
        "",
        "| # | Driver | Calls | Share | Labeling buckets | Agent |",
        "|---|---|---|---|---|---|",
        *driver_rows,
        "",
        "## Volume ranking by labeling bucket (primary reason)",
        "",
        "| # | Bucket | Calls | Share | Agent minutes | Resolved on call "
        "| Callback / escalated / unresolved |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, (bid, n) in enumerate(vol.most_common(), 1):
        oc = outcomes[bid]
        later = oc["callback_promised"] + oc["escalated"] + oc["unresolved"]
        out.append(
            f"| {i} | {names.get(bid, bid)} (`{bid}`) | {n} | {n / total:.1%} | "
            f"{mins[bid] / 60:.0f} | {oc['resolved_on_call']} | {later} |"
        )
    out += [
        "",
        "Minutes come from `duration_sec` in the call index "
        f"(total {sum(mins.values()) / 60:.0f}). "
        "Outcome columns are how Meridian's human agents handled the call (pass 1), a hint at "
        "what is resolvable in one conversation.",
        "",
        "## Secondary reasons (Opus)",
        "",
    ]
    out += [f"- {names.get(k, k)}: {v}" for k, v in sec.most_common()] or ["- none"]
    out += [
        "",
        "## Confusion pairs (labeler disagreements)",
        "",
        *([f"- {a} vs {b}: {n}" for (a, b), n in confusion.most_common()] or ["- none"]),
        "",
        "## Owner overrides",
        "",
        f"- {by_owner} (only disagreements go to the owner; there were {total - agree}).",
        "",
        "## Scheduling gaps that affect scope (I15)",
        "",
        f"- Plumbing repair calls (keyword match on `{PLUMBING_RE}`; in practice only sink and "
        f"water-heater/pilot calls match): **{plumbing}**. City known for {plumbing_known} "
        f"(from a spoken address only), of which **{plumbing_north}** are north zone, where no "
        "plumbing slots exist.",
        f"- Replacement estimate calls: **{estimates}**. City known for {estimates_known}, of "
        f"which **{estimates_austin}** are Austin (central zone), where no estimate slots exist.",
        "- City is unknown for the rest: the transcripts' account numbers and phone numbers are "
        "not in the scheduling DB (I18), so only callers who said their address are placed.",
        "",
    ]
    return rows, "\n".join(out)


def main() -> int:
    rows, report = build()
    with LABELS_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    REPORT.write_text(report)
    print(f"wrote {LABELS_CSV} ({len(rows)} rows) and {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
