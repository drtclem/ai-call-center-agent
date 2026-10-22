# Conversation eval: dev-claude-sonnet-5-5

Split `dev` (60 calls), brain `claude-sonnet-5-5`, run 2026-10-20T13:10 on commit `5e59c16`. Judge passed all sentinel conversations.

| Metric | Value |
|---|---|
| **Containment** (resolved by the agent, of all calls) | **58%** (35/60) |
| of which with fee follow-up (D10.8) | 1 (without them: 57%) |
| Accuracy on calls it should handle | 95% (of 37) |
| Handed off although it should handle | 2 |
| Abandoned (caller gave up / no end) | 0 |
| Wrong answer or wrong DB change | 0 |
| Escalated with the right reason (of must-hand-off) | 78% (of 23) |
| Escalated, any reason | 100% |
| **False containment** (kept a must-hand-off call) | **0** |
| Grounded answer rate (check C) | 91% (of 35) |
| Routing accuracy (first message) | 77% (of 39) |
| Agent cost per conversation | $0.027 |
| Whole eval cost (agent + caller + judge) | $2.14 |

## By bucket (small samples: read as direction, not rates)

| Bucket | n | contained | fee f/u | handed off (should handle) | abandoned | wrong | escalated right | other reason | false containment |
|---|---|---|---|---|---|---|---|---|---|
| billing_payments | 8 | 4 | 0 | 0 | 0 | 0 | 1 | 3 | 0 |
| complaints_disputes | 6 | 0 | 0 | 0 | 0 | 0 | 4 | 2 | 0 |
| existing_appointment_mgmt | 5 | 2 | 1 | 2 | 0 | 0 | 0 | 0 | 0 |
| maintenance_tuneup_booking | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| membership | 10 | 7 | 0 | 0 | 0 | 0 | 3 | 0 | 0 |
| non_customer_other | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| repair_service_booking | 15 | 11 | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| replacement_estimate | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| safety_emergency | 4 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| warranty | 6 | 4 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
