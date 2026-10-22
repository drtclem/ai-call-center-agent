# Retrieval eval

_Run 2026-10-20T21:02:16+00:00; 70 questions (`evals/retrieval/questions.toml`), 69 scored. Search: BM25 + `text-embedding-3-small`, merged with reciprocal rank fusion over section chunks of current customer docs (D6)._

- **Answering section in the top 3: 66/69 (96%)**; mean reciprocal rank 0.857.
- Answering section anywhere in the 4 sections the agent receives: 67/69 (97%).
- Right document in the top 3 (looser): 68/69 (99%).
- Old, stub or internal documents returned (any mode): **0**.

| Search | Section top 3 | Section top 4 | Doc top 3 | MRR |
|---|---|---|---|---|
| hybrid (used by the agent) | 66 | 67 | 68 | 0.857 |
| bm25 (baseline) | 58 | 61 | 63 | 0.708 |
| embeddings (baseline) | 64 | 65 | 69 | 0.812 |

| Group | Section hits, top 3 (hybrid) |
|---|---|
| bucket: billing | 17/18 |
| bucket: membership | 18/20 |
| bucket: scheduling | 18/18 |
| bucket: warranty | 13/13 |
| source: kb | 46/47 |
| source: paraphrase | 5/6 |
| source: transcript | 15/16 |

## Misses (hybrid, answering section not in the top 3)

- **M02** (transcript) How much is the maintenance plan? — answering section rank: 4; top 3: membership/comfort_club_overview.md#intro-0, membership/comfort_club_overview.md#upgrades-0, membership/comfort_club_overview.md#billing-and-renewal-0
- **M13** (kb) I'm a member. Do I pay the after-hours fee for an emergency call? — answering section rank: not in top 4; top 3: faq/customer_faq.md#fees-0, policies/holiday_hours_2026.png#intro-0, faq/customer_faq.md#hours-contact-0. _Trap: the FAQ fee lines say $149 with no member exception. Answer-key fix 2026-10-19: the price book (FEE-AH, waived for members) also answers it._
- **R02** (paraphrase) You hauled my old furnace away. Why am I paying extra for that? — answering section rank: not in top 4; top 3: services/service_catalog.md#hvac-repair-1, pricing/repair_price_book_2026.csv#intro-1, faq/troubleshooting_before_you_call.md#furnace-won-t-turn-on-0. _Rewording of B09._

## Unscored probes

- **P01** What fees are agents allowed to waive without a supervisor? — returned faq/troubleshooting_before_you_call.md#intro-0, faq/customer_faq.md#fees-0, faq/customer_faq.md#payment-0
