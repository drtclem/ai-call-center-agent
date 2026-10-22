# Conversation eval: final-claude-sonnet-5-5

Split `final` (360 calls), brain `claude-sonnet-5-5`, run 2026-10-20T21:26 on commit `30e6f2f`. Judge passed all sentinel conversations.

| Metric | Value |
|---|---|
| **Containment** (resolved by the agent, of all calls) | **49%** (178/360) |
| of which with fee follow-up (D10.8) | 6 (without them: 48%) |
| Accuracy on calls it should handle | 81% (of 221) |
| Handed off although it should handle | 26 |
| Abandoned (caller gave up / no end) | 0 |
| Wrong answer or wrong DB change | 17 |
| Escalated with the right reason (of must-hand-off) | 87% (of 139) |
| Escalated, any reason | 100% |
| **False containment** (kept a must-hand-off call) | **0** |
| Grounded answer rate (check C) | 84% (of 195) |
| Routing accuracy (first message) | 86% (of 236) |
| Agent cost per conversation | $0.025 |
| Whole eval cost (agent + caller + judge) | $11.97 |

## By bucket (small samples: read as direction, not rates)

| Bucket | n | contained | fee f/u | handed off (should handle) | abandoned | wrong | escalated right | other reason | false containment |
|---|---|---|---|---|---|---|---|---|---|
| billing_payments | 45 | 21 | 0 | 1 | 0 | 0 | 13 | 10 | 0 |
| complaints_disputes | 34 | 0 | 0 | 0 | 0 | 0 | 26 | 8 | 0 |
| existing_appointment_mgmt | 33 | 12 | 6 | 4 | 0 | 1 | 10 | 0 | 0 |
| maintenance_tuneup_booking | 14 | 10 | 0 | 2 | 0 | 2 | 0 | 0 | 0 |
| membership | 61 | 30 | 0 | 5 | 0 | 2 | 24 | 0 | 0 |
| non_customer_other | 14 | 8 | 0 | 1 | 0 | 0 | 5 | 0 | 0 |
| repair_service_booking | 89 | 65 | 0 | 4 | 0 | 6 | 14 | 0 | 0 |
| replacement_estimate | 10 | 9 | 0 | 1 | 0 | 0 | 0 | 0 | 0 |
| safety_emergency | 21 | 0 | 0 | 0 | 0 | 0 | 21 | 0 | 0 |
| warranty | 39 | 17 | 0 | 8 | 0 | 6 | 8 | 0 | 0 |
