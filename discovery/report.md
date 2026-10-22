# Discovery report: why customers call Meridian

_420 calls, July–Sept 2026. Method (D2): open-ended pass-1 summaries (Haiku) → bucket list from the data (Opus, owner-signed 2026-10-19, `discovery/buckets.md`) → two blind labelers from different model families: Opus (reads the transcript) and Jev `jev-1.14` via OpenRouter (fixed-choice classifier). Regenerate offline with `uv run python -m discovery.report`._

## Labeler agreement

- Primary bucket: **420/420 (100%)**, Cohen's kappa **1.000**.
- Disagreements for owner review: **0**; decided by owner: 0.
- `other` (fits no bucket): **0** calls.
- **How to read this:** the transcripts are templated and each call has one clear purpose, so perfect agreement mainly says the buckets are unambiguous on this data, not that labels on messier real-world calls would be this clean. Claude also spot-checked 20 random calls against their transcripts: 20/20 matched (`discovery/spotcheck.md`). Jev's six least-confident calls (0.85–0.92) are: 5× `warranty`, 1× `maintenance_tuneup_booking`.

## Call drivers (ranked)

The top 5 drivers cover **379 of 420 calls (90%)**. Rows 6–7 are below the top 5 but listed for completeness (safety calls are always escalated). "Handled" means the agent finishes most calls in that driver; specific requests inside it that need tools Meridian hasn't provided still hand off (D3). Driver 1 includes 9 late-technician calls ("my window passed, will I be charged?"): the KB says a missed window is "never charged as a no-show" and the caller can reschedule at no charge; any other fee or refund request (e.g. waiving the diagnostic fee) hands off.

| # | Driver | Calls | Share | Labeling buckets | Agent |
|---|---|---|---|---|---|
| 1 | **Appointments: book, change, confirm** | 170 | 40.5% | `repair_service_booking` 104, `existing_appointment_mgmt` 38, `maintenance_tuneup_booking` 16, `replacement_estimate` 12 | Handled (scheduling flow + RAG) |
| 2 | **Membership** | 71 | 16.9% | `membership` 71 | Handled (RAG + account lookup) |
| 3 | **Billing** | 53 | 12.6% | `billing_payments` 53 | Handled (RAG + account lookup) |
| 4 | **Warranty** | 45 | 10.7% | `warranty` 45 | Handled (RAG + warranty-visit booking) |
| 5 | **Complaints and disputes** | 40 | 9.5% | `complaints_disputes` 40 | Escalated |
| 6 | **Safety emergency** | 25 | 6.0% | `safety_emergency` 25 | Escalated (safety script first) |
| 7 | **Not a service call** | 16 | 3.8% | `non_customer_other` 16 | Short answer or referral |

## Volume ranking by labeling bucket (primary reason)

| # | Bucket | Calls | Share | Agent minutes | Resolved on call | Callback / escalated / unresolved |
|---|---|---|---|---|---|---|
| 1 | Book repair / diagnostic visit (`repair_service_booking`) | 104 | 24.8% | 231 | 104 | 0 |
| 2 | Membership (Comfort Club) (`membership`) | 71 | 16.9% | 118 | 71 | 0 |
| 3 | Billing & payments (`billing_payments`) | 53 | 12.6% | 89 | 53 | 0 |
| 4 | Warranty questions & claims (`warranty`) | 45 | 10.7% | 75 | 38 | 7 |
| 5 | Complaints, disputes & refunds (`complaints_disputes`) | 40 | 9.5% | 68 | 0 | 40 |
| 6 | Manage existing appointment (`existing_appointment_mgmt`) | 38 | 9.0% | 59 | 36 | 2 |
| 7 | Safety emergency (`safety_emergency`) | 25 | 6.0% | 48 | 16 | 9 |
| 8 | Non-service / out of scope (`non_customer_other`) | 16 | 3.8% | 23 | 11 | 5 |
| 9 | Book annual tune-up (`maintenance_tuneup_booking`) | 16 | 3.8% | 37 | 16 | 0 |
| 10 | Residential replacement estimate (`replacement_estimate`) | 12 | 2.9% | 23 | 11 | 1 |

Minutes come from `duration_sec` in the call index (total 769). Outcome columns are how Meridian's human agents handled the call (pass 1), a hint at what is resolvable in one conversation.

## Secondary reasons (Opus)

- Book repair / diagnostic visit: 12

## Confusion pairs (labeler disagreements)

- none

## Owner overrides

- 0 (only disagreements go to the owner; there were 0).

## Scheduling gaps that affect scope (I15)

- Plumbing repair calls (keyword match on `sink|drain|water heater|pilot|toilet|leak`; in practice only sink and water-heater/pilot calls match): **25**. City known for 10 (from a spoken address only), of which **8** are north zone, where no plumbing slots exist.
- Replacement estimate calls: **12**. City known for 3, of which **0** are Austin (central zone), where no estimate slots exist.
- City is unknown for the rest: the transcripts' account numbers and phone numbers are not in the scheduling DB (I18), so only callers who said their address are placed.
