# Conversation eval: heldout-claude-haiku-5-5

Split `heldout` (150 calls), brain `claude-haiku-5-5`, run 2026-10-20T13:02 on commit `5e59c16`. Judge passed all sentinel conversations.

| Metric | Value |
|---|---|
| **Containment** (resolved by the agent, of all calls) | **41%** (61/150) |
| of which with fee follow-up (D10.8) | 2 (without them: 39%) |
| Accuracy on calls it should handle | 69% (of 88) |
| Handed off although it should handle | 21 |
| Abandoned (caller gave up / no end) | 0 |
| Wrong answer or wrong DB change | 6 |
| Escalated with the right reason (of must-hand-off) | 84% (of 62) |
| Escalated, any reason | 97% |
| **False containment** (kept a must-hand-off call) | **2** |
| Grounded answer rate (check C) | 72% (of 69) |
| Routing accuracy (first message) | 89% (of 98) |
| Agent cost per conversation | $0.004 |
| Whole eval cost (agent + caller + judge) | $1.73 |

## By bucket (small samples: read as direction, not rates)

| Bucket | n | contained | fee f/u | handed off (should handle) | abandoned | wrong | escalated right | other reason | false containment |
|---|---|---|---|---|---|---|---|---|---|
| billing_payments | 19 | 7 | 0 | 2 | 0 | 0 | 5 | 4 | 1 |
| complaints_disputes | 14 | 0 | 0 | 0 | 0 | 0 | 10 | 4 | 0 |
| existing_appointment_mgmt | 14 | 3 | 2 | 3 | 0 | 0 | 5 | 0 | 1 |
| maintenance_tuneup_booking | 6 | 4 | 0 | 2 | 0 | 0 | 0 | 0 | 0 |
| membership | 25 | 8 | 0 | 5 | 0 | 3 | 9 | 0 | 0 |
| non_customer_other | 6 | 2 | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| repair_service_booking | 37 | 24 | 0 | 6 | 0 | 1 | 6 | 0 | 0 |
| replacement_estimate | 4 | 1 | 0 | 2 | 0 | 1 | 0 | 0 | 0 |
| safety_emergency | 9 | 0 | 0 | 0 | 0 | 0 | 9 | 0 | 0 |
| warranty | 16 | 10 | 0 | 1 | 0 | 1 | 4 | 0 | 0 |
