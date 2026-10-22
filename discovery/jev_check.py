"""Spike S1 (T1): can we call Jev (via OpenRouter)? Classifies 5 transcripts.

Run: uv run --env-file .env python -m discovery.jev_check
Prints call IDs, labels, scores and the model version only — never the key.
"""

import sys
from pathlib import Path

import httpx

from discovery.jev import classify

TRANSCRIPTS = Path(__file__).resolve().parents[1] / "data/meridian_dataset/call_transcripts"
SAMPLE = [
    "CALL-20260818-0206",  # sink backing up, books a visit
    "CALL-20260729-0041",  # complaint about a technician
    "CALL-20260913-0382",  # membership renewal question
    "CALL-20260730-0183",  # confirm an existing appointment
    "CALL-20260709-0134",  # system replacement estimate
]
DRAFT_BUCKETS: dict[str, str | None] = {
    "book_new_visit": "Wants a technician to come out for a repair, tune-up or estimate",
    "existing_appointment": "Confirm, reschedule, cancel or ask about a booked appointment",
    "membership_or_pricing": "Questions about Comfort Club, prices, fees or warranty",
    "billing": "Questions or disputes about an invoice, balance or charge",
    "complaint_or_manager": "Complaint about service or a technician, or asks for a manager",
    "emergency": "Gas smell, CO alarm, sparks, flooding or other urgent safety issue",
}
INSTRUCTIONS = "Why did this customer call Meridian Home Comfort? Pick the primary reason."


def main() -> int:
    with httpx.Client() as client:
        for call_id in SAMPLE:
            text = (TRANSCRIPTS / f"{call_id}.txt").read_text()
            try:
                c = classify(client, text, INSTRUCTIONS, DRAFT_BUCKETS)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                print(f"{call_id}: FAILED {type(exc).__name__}: {exc}")
                return 1
            top = sorted(c.probabilities.items(), key=lambda kv: -kv[1])[:2]
            print(f"{call_id}: {c.label} (confidence {c.confidence:.2f}; top {top}) [{c.model}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
