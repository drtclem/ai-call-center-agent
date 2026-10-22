# Discovery: bucket list (DRAFT — for review and owner sign-off)

_Proposed by `claude-opus-5-5` from 420 pass-1 lines (`discovery/cache/pass1.jsonl`, Haiku, no category list given). Cross-check: `text-embedding-3-small` + cosine k-means; each cluster is matched to the bucket whose definition is closest. Counts are the proposer's estimates (they sum to 416, not 420); real counts come from the two blind labelers in T3._

## Proposed buckets

| id | name | est. calls | definition | includes | excludes | examples |
|---|---|---|---|---|---|---|
| `repair_service_booking` | Book repair / diagnostic visit | 105 | Customer reports a non-emergency equipment or plumbing problem and wants a technician scheduled. | Burning smell from vents, AC blowing warm or not cooling, furnace won't turn on, blank thermostat, grinding noise, water pooling at indoor unit, backed-up kitchen sink, water heater pilot going out. Also covers waitlist requests and questions about whether the maintenance plan would lower the cost of this repair. | Gas, CO or sparks emergencies. Warranty-coverage questions on failed equipment. Annual tune-ups. | CALL-20260706-0101, CALL-20260709-0103, CALL-20260709-0365 |
| `maintenance_tuneup_booking` | Book annual tune-up | 17 | Customer wants to schedule routine annual HVAC maintenance. | Annual tune-up scheduling, including waitlist requests. | Visits for a reported problem. | CALL-20260711-0109, CALL-20260720-0371, CALL-20260801-0176 |
| `existing_appointment_mgmt` | Manage existing appointment | 41 | Customer is calling about an appointment that is already booked. | Reschedules, cancellations because the issue was already fixed, confirming an appointment window, and technician no-shows (window passed with no arrival or call). | New bookings. | CALL-20260711-0063, CALL-20260707-0002, CALL-20260721-0261 |
| `billing_payments` | Billing & payments | 52 | Routine account balance, payment, or charge-explanation requests that are not disputes. | Balance lookups, paying an invoice (via a texted secure link), explaining an unrecognized invoice and emailing an itemized copy, and suspected double charges. | Disputes over quoted vs. invoiced price. Refund demands. Membership autopay. | CALL-20260708-0255, CALL-20260709-0356, CALL-20260710-0373 |
| `membership` | Membership (Comfort Club) | 64 | Questions or account changes about the maintenance membership. | Plan info and price (including the $149 check), renewal charge timing, cancelling membership, autopay setup, upgrading to Plus, and member discount on a new system. | Questions about whether the plan would save money on a repair being booked; those go to the repair bucket. | CALL-20260707-0148, CALL-20260707-0096, CALL-20260710-0364 |
| `warranty` | Warranty questions & claims | 50 | Questions about warranty coverage, registration, transfer, or filing a claim. | Warranty claims on last year's install, compressor coverage (often followed by booking a repair), labor warranty on a repeat blower failure, manufacturer registration status, and warranty transfer to a new homeowner. | Generic repairs with no warranty question. | CALL-20260711-0248, CALL-20260707-0053, CALL-20260722-0161 |
| `replacement_estimate` | Residential replacement estimate | 15 | Customer wants a ballpark cost for replacing an aging home system and/or a free in-home estimate. | Replacement of an 18-year-old system, ballpark price, and booking a comfort advisor visit. | Commercial quotes. Member discount questions. | CALL-20260709-0134, CALL-20260711-0030, CALL-20260721-0321 |
| `complaints_disputes` | Complaints, disputes & refunds | 47 | Customer is dissatisfied and needs human or manager resolution. | Invoice higher than the quote (after-hours, disposal or permit fees), refund for a failed repair, disputed $4,000 replacement recommendation, rude technician, and repeated unreturned calls. | Neutral invoice questions or double-charge checks. | CALL-20260714-0246, CALL-20260710-0136, CALL-20260718-0062 |
| `safety_emergency` | Safety emergency | 9 | Potentially hazardous situation needing immediate safety guidance and dispatch. | Gas smell, carbon monoxide alarm, sparks from the outdoor unit. | Burning smell from vents, which these lines treat as a routine booking. | CALL-20260712-0046, CALL-20260721-0093, CALL-20260717-0180 |
| `non_customer_other` | Non-service / out of scope | 16 | Calls not about residential service for an existing or prospective customer. | Job/hiring inquiries, wrong number (Meridian Dental), and commercial rooftop replacement quotes. | Any residential service need. | CALL-20260712-0067, CALL-20260813-0031, CALL-20260728-0089 |

**Proposer's notes:** Counts are approximate. 1) Burning smell from vents is bucketed as a routine repair because every one of these calls was booked normally; decide whether policy should treat it as a safety emergency. 2) Technician no-shows are placed in appointment management, though one asked for a fee waiver and could count as a complaint. 3) Compressor-warranty and repeat-blower calls usually end in a booking; they are classified by the opening warranty question. 4) Membership autopay could go under billing instead; it is placed under membership. 5) Commercial quotes are grouped with out-of-scope; split them out if commercial is a real line of business. 6) Several lines mention a 'Trane' or 'Goodman' unit on kitchen-sink calls, which suggests transcript or summary noise. 7) All complaints and safety calls ended in a callback or escalation, while most other buckets resolved on the call. That points to the first seven buckets as the AI-automatable set, though registration, warranty transfer, and replacement-estimate calls sometimes needed callbacks.

## Cross-check: embedding clusters

A cluster whose closest bucket scores below 0.45 is flagged ⚠️ (possibly something the bucket list doesn't explain).

**Cluster 1** (69 calls) → `repair_service_booking` (0.75)
- CALL-20260706-0182: Customer smelled something burning coming from the vents and wanted an HVAC service call scheduled as soon as possible.
- CALL-20260709-0082: Customer smelled something burning coming from the vents and wanted a technician to inspect the system as soon as possible.
- CALL-20260710-0028: Customer wanted to book a repair because they smelled something burning coming from the vents of their HVAC system.
- CALL-20260710-0081: Customer's AC is blowing warm and he wanted a technician dispatched; he also asked whether joining the maintenance plan would save him money on that repair.
- CALL-20260712-0046: Customer smelled gas near their furnace and called in urgently, and the agent told them to evacuate and contact the gas utility or 911 first.

**Cluster 2** (67 calls) → `membership` (0.71)
- CALL-20260707-0096: Customer received an email about her service membership renewing and asked when it would actually charge.
- CALL-20260707-0148: Customer called to ask about the Comfort Club maintenance membership: what it includes, its price, and whether it covers plumbing.
- CALL-20260707-0392: Customer received an email about his membership renewing and wanted to know when it would actually be charged.
- CALL-20260709-0347: Customer wanted to set up automatic payments for his membership account.
- CALL-20260710-0135: Customer received an email saying his service membership is renewing and wanted to know when the charge would actually hit.

**Cluster 3** (53 calls) → `maintenance_tuneup_booking` (0.67)
- CALL-20260706-0101: Customer smelled something burning from the vents and wanted to schedule an HVAC service call, preferring Thursday 8–12.
- CALL-20260707-0170: Customer's thermostat is blank and he wanted to schedule a service visit, preferring Tuesday 4-7 PM after work.
- CALL-20260709-0103: Customer's furnace won't turn on, so they called to book a repair visit for tomorrow between 12 and 4.
- CALL-20260711-0109: Customer wanted to schedule his annual HVAC tune-up and asked for the soonest available technician, preferably Friday morning.
- CALL-20260711-0380: Customer's thermostat is blank and he wants a repair appointment as soon as possible, preferably first thing in the morning.

**Cluster 4** (47 calls) → `complaints_disputes` (0.64)
- CALL-20260708-0115: Customer wanted to pay invoice INV-263891 by phone and asked whether that was possible.
- CALL-20260709-0356: Customer wanted to pay invoice INV-213004 by phone and was sent a secure text payment link after the agent verified her name and phone number on the account.
- CALL-20260710-0335: Customer disputes a $4,000 system replacement quoted by a technician to his wife, after a second opinion found it was a $200 capacitor, and wants to speak with someone in charge.
- CALL-20260710-0373: Customer received invoice INV-292142 for $245 and did not recognize the charge, so he called to understand what it was for.
- CALL-20260714-0246: Customer disputes a service invoice of $149 after being quoted $89 by the technician, saying the after-hours charge was never disclosed.

**Cluster 5** (45 calls) → `repair_service_booking` (0.63)
- CALL-20260709-0134: Customer's 18-year-old HVAC system is nearing end of life and he wanted a ballpark cost for a full replacement, then booked a free in-home estimate.
- CALL-20260711-0030: Customer wants to know the cost of replacing his 18-year-old HVAC system and asked for a free in-home estimate.
- CALL-20260711-0248: Customer's installed unit (installed last year) stopped working and he wanted to file a warranty claim.
- CALL-20260712-0227: Customer wanted to file a warranty claim because the HVAC unit installed by the company last year stopped working.
- CALL-20260714-0326: Customer wants to file a warranty claim because the air conditioning/HVAC unit Meridian installed last year stopped working.

**Cluster 6** (35 calls) → `warranty` (0.66)
- CALL-20260707-0053: Customer asked whether his newly installed HVAC unit had been registered with the manufacturer, since he never received a confirmation.
- CALL-20260709-0010: Customer asked whether her newly installed HVAC unit had been registered with the manufacturer, since she never received confirmation.
- CALL-20260709-0315: Customer's three-year-old Trane system's compressor failed and she wanted to know whether it was covered under warranty.
- CALL-20260712-0007: Customer asked whether the labor warranty covers a repeat repair, since the blower her company fixed in March is failing the same way again, and she wanted a visit scheduled.
- CALL-20260712-0195: Customer asked whether the company had registered her newly installed HVAC unit with the manufacturer, since she never received confirmation.

**Cluster 7** (33 calls) → `existing_appointment_mgmt` (0.58)
- CALL-20260709-0365: Customer called to book a repair for a backed-up kitchen sink, preferably Thursday 4-7 PM after work.
- CALL-20260711-0063: Customer needed to move a technician appointment scheduled for tomorrow because something came up, and asked for an afternoon slot this week.
- CALL-20260718-0360: Customer wanted to book a repair because her kitchen sink is backing up.
- CALL-20260720-0216: Customer's kitchen sink is backing up and he wanted a technician sent out to look at it.
- CALL-20260721-0258: Customer needed to reschedule a technician visit scheduled for the next day because something came up.

**Cluster 8** (26 calls) → `complaints_disputes` (0.64)
- CALL-20260710-0124: Customer called to confirm his Thursday appointment window (12 to 4) for a water heater inspection at his home.
- CALL-20260718-0062: Customer wants a manager after a technician visit the previous day, citing rudeness, tracking mud through the house, and leaving without explaining the work.
- CALL-20260721-0261: Customer's scheduled service window (12–4 PM) had passed with no technician arriving and no call, so she asked whether she should keep waiting.
- CALL-20260722-0198: Customer complained that the technician who visited the previous day was rude, tracked mud through the house, and left without explaining the work, and asked to speak with a manager.
- CALL-20260724-0391: Customer has called three times about an unresolved problem with no one calling back, and is frustrated enough to say she's done.

**Cluster 9** (15 calls) → `billing_payments` (0.57)
- CALL-20260708-0255: Customer called to ask for the current balance on his account, which the agent looked up and reported as $245 on invoice INV-233442, due the 6th.
- CALL-20260710-0121: Customer called to find out his current account balance.
- CALL-20260715-0021: Customer called to find out her current account balance.
- CALL-20260718-0419: Customer called to find out his current account balance.
- CALL-20260727-0080: Customer called to find out his current account balance, and the agent verified his identity and gave him the $1,240 open balance on invoice INV-257292.

**Cluster 10** (14 calls) → `complaints_disputes` (0.61)
- CALL-20260707-0002: Customer called to cancel a scheduled service appointment because her brother-in-law had already fixed the issue herself.
- CALL-20260710-0136: Customer wants a full refund for a repair that did not fix the problem, forcing him to hire another company.
- CALL-20260715-0100: Customer wants a full refund for a recent repair that did not fix the problem, forcing her to call another company.
- CALL-20260724-0325: Customer called to cancel an upcoming service appointment because a family member had already fixed the problem.
- CALL-20260726-0220: Customer called to cancel a scheduled service appointment because a family member had already fixed the issue.

**Cluster 11** (11 calls) → `billing_payments` (0.49)
- CALL-20260709-0129: Customer believed she was double-charged two $1240 amounts on her card by the company and wanted it checked.
- CALL-20260725-0288: Customer believed he was double-charged $245 twice on his card by the company and wanted to confirm.
- CALL-20260726-0331: Customer believed she was double-charged two $312 amounts on her card and wanted to know if she was actually being billed twice.
- CALL-20260806-0207: Customer believed he was double-charged two $312 amounts on his card by the company and wanted to know if he was being billed twice.
- CALL-20260810-0219: Customer believed he was double-charged two $189 amounts on his card by the company and wanted it checked.

**Cluster 12** (5 calls) → `non_customer_other` (0.63)
- CALL-20260813-0031: Caller dialed a different business (Meridian Dental) by mistake and had no HVAC or plumbing need.
- CALL-20260820-0374: Caller dialed by mistake, asking whether this was Meridian Dental, and was not seeking HVAC or plumbing service.
- CALL-20260904-0186: Caller dialed the wrong business, apparently looking for Meridian Dental, and was not seeking HVAC or plumbing service.
- CALL-20260906-0280: Caller dialed by mistake, apparently looking for Meridian Dental, not an HVAC service.
- CALL-20260918-0417: Caller dialed a wrong number, apparently intending Meridian Dental, and reached the HVAC company by mistake.
