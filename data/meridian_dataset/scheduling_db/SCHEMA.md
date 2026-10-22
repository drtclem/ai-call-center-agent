# Scheduling Database — Schema

SQLite file: `meridian_scheduling.sqlite`. The same tables are exported as CSV alongside it.

## Tables

### customers
| column | type | notes |
|---|---|---|
| customer_id | TEXT PK | Format `MHC-#####` |
| name, phone, email | TEXT | |
| service_address, city, zip | TEXT | |
| membership_tier | TEXT | `none`, `standard`, `plus` |
| membership_renewal_date | TEXT | ISO date, NULL if none |
| system_brand, system_install_date | TEXT | |

### technicians
| tech_id | name | skills (`hvac`, `plumbing`, `estimate`; comma-separated) | home_zone (`north`,`central`,`south`) |

### appointment_slots
One row per technician × date × window × service type.
| column | notes |
|---|---|
| slot_date | ISO date |
| window | `08:00-12:00`, `12:00-16:00`, `13:00-17:00`, `16:00-19:00` (Tue/Thu only) |
| zone | which zone the tech covers that day; customers should be matched to their city's zone |
| service_type | `hvac_repair`, `hvac_tune_up`, `plumbing`, `estimate` |
| status | `open` (bookable), `booked`, `reserved_members` (bookable only for standard/plus members), `blocked` |

### appointments
| column | notes |
|---|---|
| slot_id | FK → appointment_slots |
| customer_id | FK → customers |
| status | `scheduled`, `completed`, `cancelled`, `no_show` |
| is_warranty_visit | 1 if no diagnostic fee applies |

### invoices
| column | notes |
|---|---|
| amount_cents / balance_cents | integers in cents; balance 0 = paid |
| line_items | JSON array of `{desc, cents}` |

## Zone mapping (city → zone)
north: Round Rock, Cedar Park, Pflugerville, Georgetown, Leander, Hutto · central: Austin · south: Kyle, Buda

## Expected agent behavior
- **Check availability**: find `open` slots (or `reserved_members` if the customer is a member) matching the customer's zone, the requested service_type, and the requested date/window.
- **Book**: set the slot to `booked` and insert an `appointments` row. Return a confirmation with date, window, and tech name.
- **Reschedule / cancel**: update the existing appointment and free the old slot (`open`).
- **Confirm**: look up scheduled appointments for the customer.
- Emergency calls (safety) are NOT booked here — they escalate.

## Seeded test customers
| customer_id | name | phone | tier | notes |
|---|---|---|---|---|
| MHC-10001 | Linda Foster | 512-555-0101 | standard | Has an appointment 2026-10-22 08:00-12:00 (AC blowing warm) |
| MHC-10002 | Omar Patel | 512-555-0102 | none | Has open invoice INV-204411 for $478.00 |
| MHC-10003 | Grace Okafor | 512-555-0103 | plus | Lennox installed 2025-11-30 (under 2-yr install labor warranty) |
| MHC-10004 | Daniel Reyes | 512-555-0104 | none | Carrier installed 2026-07-09 |
