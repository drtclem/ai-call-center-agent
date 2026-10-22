# Knowledge base and operations issues for Meridian

_For Meridian's operations team. Found while building the AI agent (October 2026). Each item says what we found, what the AI does about it today, and the fix we recommend. Details and evidence: `docs/decisions.md` (D3, D5, D10, D11) and `discovery/report.md`._

## Documents to clean up

| # | Issue | What the AI does today | Recommended fix |
|---|---|---|---|
| 1 | **Old Comfort Club sheet** (`archive/comfort_club_overview.md`) still says $149/yr, 10% off, refundable any time, transfers to a new address. Callers quote "$149" (8+ calls). | Never uses it; answers $189/yr or $16.99/mo and explains the January 2026 change. | Delete or clearly mark the archive copy. |
| 2 | **2023 warranty terms** (90-day repair labor, 1-year install) next to the 2025 policy (1-year repair, 2-year install, 5-year for members). | Uses only the 2025 policy. | Delete the 2023 file. |
| 3 | **Old FAQ copy** (diagnostic $79, after-hours $129, hours 8–6, checks accepted). | Uses only the March 2026 FAQ. | Delete `faq_old_copy.md`. |
| 4 | **Cancellation notice conflict:** the agent scheduling guide (rev 2024-11) says 48 hours; the 2026 FAQ and fee schedule say 24 hours ($49 inside 24 h). | Follows 24 hours. | Update the scheduling guide. |
| 4b | **"Offer the waitlist"**: the scheduling guide says to offer a waitlist when nothing is open within 48 hours, but the scheduler has no waitlist and no customer document mentions one. | Says it can't add anyone to a waitlist here and suggests checking back for earlier openings; it doesn't claim either way whether Meridian has one (T15 finding). | Add a waitlist to the scheduler, or remove the line. |
| 5 | **"Same-day overflow" for emergencies** in the scheduling guide; the scheduler has no such slots and the booking rules say emergencies are not booked. | Hands emergencies to a person. | Remove the line or add overflow slots. |
| 6 | **Duct cleaning page is a stub** ("TODO — Marcy to write up partner referral process"). | Says duct cleaning isn't offered and hands off for a partner referral (from the service catalog). | Write the referral process. |
| 7 | **Holiday hours exist only as an image**; the fee schedule only as a PDF. | Reads converted text versions. | Keep text versions on the shared drive. |
| 8 | **Service area vs. ZIP codes:** "Austin: 787xx (all)" but Lakeway (also 787xx) is "not serviced". | Asks Lakeway callers east or west of 620; hands off if unclear. | Replace with an explicit ZIP list. |
| 8a | **The FAQ's service-area list omits Manor and Dripping Springs**, added in April 2026. | Searches a copy of the FAQ without that line; uses the newer service-area page. | Update the FAQ. |
| 8b | **Staff-only discretion inside a customer document:** the technician-arrival page tells agents they may waive the diagnostic fee for late technicians. | Reads a customer version with those lines removed; the AI never waives fees and hands those requests to a person. | Move staff discretion to the internal notes. |
| 8c | **"4-hour windows"** in the arrival page, but the Tue/Thu 4–7pm window is 3 hours. | Quotes the actual window from the scheduler. | Correct the page. |
| 8d | **Holiday dispatch fee ($199) vs. members:** the membership page says members pay "no after-hours dispatch fee", but neither it nor the fee schedule says whether the holiday fee is waived. | Doesn't claim the holiday fee is waived for members; hands off if asked. | State it explicitly. |
| 8e | **The FAQ's fee list says after-hours dispatch is $149 with no member exception** (it notes the member waiver only for the diagnostic fee); the membership page and fee schedule say members pay nothing. A member asking about after-hours fees can be shown the FAQ line first. | Flagged as a known retrieval miss (eval M13); handled in the agent design (T10/T11). | Add "waived for Comfort Club members" to the FAQ's after-hours line. |
| 8f | **"New Year's Day (Jan 1)" on a 2026 holiday sheet** is ambiguous (Jan 1, 2026 has passed; Jan 1, 2027 is next). | Quotes the sheet as written. | Date the sheet's entries with years. |

## Operational gaps that cap what the AI can handle (59–68 calls, 14–16%)

| # | Gap | Calls affected | What the AI does today | Recommended fix |
|---|---|---|---|---|
| 9 | **No plumbing technicians scheduled in the north zone** (Round Rock, Cedar Park, Pflugerville, Georgetown, Leander, Hutto), where 70% of customers (108 of 154) live. | 25 plumbing repair calls; 10 have a known city, 8 of them north. If the rest follow the same split, ~17. | Hands off; never books another zone. | Add north-zone plumbing slots. |
| 10 | **No estimate slots in Austin proper** (central zone). | None confirmed in the calls (3 of 12 estimate callers have a known city; none in Austin), but any Austin estimate request can't be booked. | Hands off. | Add central-zone estimate slots. |
| 11 | **Manor and Dripping Springs** were added to the service area (April 2026) but have no scheduling zone. | — | Says "yes, we service you" and hands off the booking. | Map both to a zone. |
| 12 | **Payment links:** callers asking to pay an invoice need a secure link the AI can't send. | 14 calls | Hands off. | Expose a "send payment link" action to the AI. |
| 13 | **Membership changes:** autopay setup and membership cancellation need account changes the AI can't make. | 27 calls | Hands off. | Expose those actions (with confirmation) to the AI. |
| 14 | **Unit registration status** isn't recorded anywhere the AI can read. | 10 calls | Hands off. | Record manufacturer registration dates per install. |

## Safety and quality findings from the call recordings

| # | Finding | What the AI does |
|---|---|---|
| 15 | **All 16 "burning smell from the vents" calls were booked as routine repairs**, some days out. Meridian's safety protocol requires leave-the-home for an electrical burning smell *combined with tripped breakers*; callers weren't asked about breakers. | Deliberately stricter than the protocol (owner decision): treats any burning smell as a safety emergency, reads the safety script and hands off immediately. |
| 16 | **One agent quoted the $89 diagnostic fee to a tune-up caller** (tune-ups are $129, no diagnostic fee), and another offered a "morning window, 12 to 4". | Quotes only from the current documents and the scheduler. |
| 17 | **Account numbers and phone numbers spoken in the recordings don't match the customer database.** | Verifies identity against the database only (name + account number, phone, or address). |
