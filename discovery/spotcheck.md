# Discovery spot-check (Claude, 2026-10-19)

20 calls drawn with `random.seed(19); random.sample(sorted(call_ids), 20)`. For each, Claude read the transcript and compared it with the final label. Result: **20/20 match**.

| call_id | final label | match |
|---|---|---|
| CALL-20260906-0283 | `complaints_disputes` | ✅ |
| CALL-20260710-0124 | `existing_appointment_mgmt` | ✅ |
| CALL-20260915-0303 | `billing_payments` | ✅ |
| CALL-20260822-0212 | `repair_service_booking` | ✅ |
| CALL-20260715-0351 | `non_customer_other` | ✅ |
| CALL-20260821-0199 | `billing_payments` | ✅ |
| CALL-20260722-0198 | `complaints_disputes` | ✅ |
| CALL-20260810-0210 | `repair_service_booking` | ✅ |
| CALL-20260806-0174 | `membership` | ✅ |
| CALL-20260823-0022 | `repair_service_booking` | ✅ |
| CALL-20260730-0274 | `warranty` | ✅ |
| CALL-20260828-0267 | `complaints_disputes` | ✅ |
| CALL-20260718-0262 | `billing_payments` | ✅ |
| CALL-20260829-0404 | `membership` | ✅ |
| CALL-20260728-0094 | `repair_service_booking` | ✅ |
| CALL-20260714-0366 | `non_customer_other` | ✅ |
| CALL-20260811-0305 | `repair_service_booking` | ✅ |
| CALL-20260803-0155 | `replacement_estimate` | ✅ |
| CALL-20260728-0281 | `repair_service_booking` | ✅ |
| CALL-20260913-0382 | `membership` | ✅ |
