# Conversation eval: heldout-claude-sonnet-5-5

Split `heldout` (150 calls), brain `claude-sonnet-5-5`, run 2026-10-19T22:03 on commit `4243fc6`. Judge passed all sentinel conversations.

| Metric | Value |
|---|---|
| **Containment** (resolved by the agent, of all calls) | **48%** (72/150) |
| of which with fee follow-up (D10.8) | 2 (without them: 47%) |
| Accuracy on calls it should handle | 82% (of 88) |
| Handed off although it should handle | 13 |
| Abandoned (caller gave up / no end) | 0 |
| Wrong answer or wrong DB change | 3 |
| Escalated with the right reason (of must-hand-off) | 66% (of 62) |
| Escalated, any reason | 100% |
| **False containment** (kept a must-hand-off call) | **0** |
| Grounded answer rate (check C) | 77% (of 75) |
| Routing accuracy (first message) | 86% (of 98) |
| Agent cost per conversation | $0.025 |
| Whole eval cost (agent + caller + judge) | $4.89 |

## By bucket (small samples: read as direction, not rates)

| Bucket | n | contained | fee f/u | handed off (should handle) | abandoned | wrong | escalated right | other reason | false containment |
|---|---|---|---|---|---|---|---|---|---|
| billing_payments | 19 | 7 | 0 | 2 | 0 | 0 | 5 | 5 | 0 |
| complaints_disputes | 14 | 0 | 0 | 0 | 0 | 0 | 10 | 4 | 0 |
| existing_appointment_mgmt | 14 | 4 | 2 | 2 | 0 | 0 | 0 | 6 | 0 |
| maintenance_tuneup_booking | 6 | 5 | 0 | 1 | 0 | 0 | 0 | 0 | 0 |
| membership | 25 | 13 | 0 | 2 | 0 | 1 | 9 | 0 | 0 |
| non_customer_other | 6 | 1 | 0 | 1 | 0 | 0 | 4 | 0 | 0 |
| repair_service_booking | 37 | 25 | 0 | 5 | 0 | 1 | 0 | 6 | 0 |
| replacement_estimate | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| safety_emergency | 9 | 0 | 0 | 0 | 0 | 0 | 9 | 0 | 0 |
| warranty | 16 | 11 | 0 | 0 | 0 | 1 | 4 | 0 | 0 |

## Correction (2026-10-19, after the T15 re-review; not a new run)

Two grader bugs were found after this run. Both are fixed in the next commit; this run was not repeated (owner's call, to save cost), so these figures are corrected by hand:
- **Escalated with the right reason: 85% (53/62), not 66%.** 12 must-hand-off cases (9 "can't be booked here", 3 "late fee declined") handed off with the expected reason but were graded against the booking/change their request kind implied. None of their tool results show a booking, move or cancellation, so the DB was untouched and they count as right.
- **Grounded answer rate (77%) understates.** The judge couldn't see the code-written late-change fee text as a source, so a correct "$49" was marked ungrounded. Not re-judged; the next full run gives the real figure.
