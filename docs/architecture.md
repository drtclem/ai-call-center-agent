# Architecture — Meridian AI Call Center Agent

_Owner: Taylor Clements. Status: approved with decisions D1–D20 (all decided; D3 and D4 on 2026-10-19). **Changes need owner approval.** The **Rules** section is binding: the reviewer treats a violation as Blocking._
_Last updated: 2026-10-20 — factual corrections to match the code (layout, tables, tools, flows, tests); rules unchanged_

## 1. Stack

Exact versions are pinned in `pyproject.toml` / `uv.lock` (e.g. FastAPI 0.146.0, LangGraph 1.2.16, anthropic 1.15.0, openai 3.35.1), checked against current releases (workflow rule: verify versions and APIs from official docs; don't rely on memory).

| Piece | Choice | Decision |
|---|---|---|
| Language / tooling | Python 3.14, `uv`, ruff (lint + format), pyright strict, pytest (+ Hypothesis for invariants) | boostlingo defaults |
| Web | FastAPI + Jinja2 templates + HTMX; one hand-written stylesheet; no Node | D12 |
| Agent | LangGraph (1.x) supervisor + workers | D8 |
| Models | Claude Sonnet 5.5 (`claude-sonnet-5-5`) workers; Claude Haiku 5.5 (`claude-haiku-5-5`) supervisor, simulated eval caller and eval comparison; OpenAI `gpt-6-astra` backup for both roles; OpenAI `text-embedding-3-small` | D7 |
| Discovery | Haiku (pass 1), Opus 5.5 (bucket list, labeler 1), Jev (labeler 2; fallback fast LLM), OpenAI flagship (suggested pick) | D2, D7 |
| Database | SQLite: Meridian's tables + our app tables, one file on the Railway volume | D13 |
| Search index | Section embeddings built offline (`kb/build_index.py`) and committed in `kb/index/`; BM25 and the sections rebuilt at startup and checked against the committed hashes; no vector DB | D6 |
| Hosting | Railway: one service + one volume; Dockerfile; deploy from GitLab CI (`railway up`) | D13 |
| CI | GitLab CI on the owner's ARM Mac runner (Docker executor; Docker Desktop must be running); scheduled pipelines run the T18 alert jobs | D1, D15 |
| Tracing | Own tables + dashboard; Langfuse optional on the owner's Mac only | D15 |

## 2. Repo layout

```
src/meridian/
  config.py            settings from env (ENV, APP_TODAY, DB_PATH, STAFF_PASSWORD); model keys are read by the SDKs
  clock.py             the injected "today" (frozen 2026-10-16), now() at real Central time, ops time for limits — the only source of dates
  db/                  connection, app-table migrations, first-boot copy of the dataset DB
  kb/                  manifest loader, chunker, hybrid search (index load + BM25 + embeddings)
  identity/            verify caller, session lock
  scheduling/          zone map, slot search, book/reschedule/cancel, appointments, invoices, membership, warranty, fee consent + fee review
  agent/
    state.py           shared graph state
    graph.py           LangGraph wiring: code gate, supervisor (routes to kb / scheduling / handoff),
                       KB and scheduling worker loops, handoff node, fee-consent step
    tools.py           tool schemas per worker and the executor that validates and runs them
    confirm.py         did the caller agree to a specific offered slot (code, D10.6)
    safety.py          code safety / crisis / person-request check (first step)
    workers/           empty package (workers live in graph.py)
    handoff.py         reasons, urgency and the caller message (code, not a model); the ticket row is written by monitoring/records.py
    verify_answer.py   D19 checks A + B, canary phrase block
    llm.py             model adapter: Anthropic primary, OpenAI backup, fake for tests
    prompts/           instruction files (versioned in git)
  web/                 app.py (chat, consent, rating, /health), staff.py (staff pages), templates/, static/ (css, htmx)
  limits.py            D14 rate limits and spend cap
  monitoring/          records.py (conversations, turns, tickets, ratings), metrics.py (dashboard queries, masking)
  feedback/            flags, reviews, add-as-test, resolution codes
discovery/             discovery pipeline scripts, cache/, outputs (labels.csv, buckets.md, report.md)
kb/                    manifest.toml, convert.py → converted/ (text from pdf/png/csv), build_index.py → index/
evals/
  retrieval/           questions.toml, runner, results/
  conversations/       build.py, cases/ (dev, heldout, final .jsonl; D4), dev/ (feedback cases), run.py (routing accuracy is one of its metrics), sentinels.jsonl (grader known-answers), import_feedback.py, results/
  redteam/             cases.toml, disguise transforms, runner, results/
tests/                 unit and integration tests (fake model; temp DB copies)
data/meridian_dataset/ the dataset as given — READ-ONLY
docs/                  brief, decisions, prd, architecture, requirements, tasks, deploy, ...
scripts/               ci.sh (local mirror of .gitlab-ci.yml), entrypoint.sh, live_checks.sh, alert_check.py, llm_smoke.py
Dockerfile, .gitlab-ci.yml, pyproject.toml, uv.lock, .env.example
```

## 3. Rules (binding; violations are Blocking)

1. **Dataset is read-only.** Nothing writes under `data/meridian_dataset/`. The app writes only to its runtime DB copy; tests use temp copies.
2. **One clock.** All dates come from `meridian.clock`; no `date.today()`, `datetime.now()` or `time.time()` for business dates (monotonic clocks are fine for latency).
3. **Model SDKs only in `agent/llm.py`** (and `discovery/` and `kb/` converters for offline steps). Everything else depends on the adapter.
4. **DB writes only in `scheduling/`, `db/`, `monitoring/`, `feedback/` and `limits.py`.** SQL is parameterized; no string formatting.
5. **Meridian's writes are atomic and conditional:** slot status changes check the current status and the row count, in one transaction with the appointment change (D10).
6. **Customer lock:** every account tool takes the customer ID from the verified session, never from model arguments (D9, D20).
7. **No money tools.** No code path refunds, waives, discounts or charges. Fee reviews are records for staff (D5, D10).
8. **No KB facts in code or prompts.** Prices, hours and policies come from retrieval; prompts hold rules, not facts (workflow rule).
9. **Only manifest-listed, current, customer-audience docs are indexed** (D5).
10. **Every model-written reply passes `verify_answer`** before it is sent (D19): check A (numbers and dates in sources) on all of them, check B (quoted KB sections) on KB answers. Code-written texts (read-backs, fee notices, handoff messages) are built from DB and KB values and aren't re-checked.
11. **The safety check runs first, in code,** before any model call on every message (D8, D11).
12. **Every model call has a timeout**; failure → retry once → backup → polite handoff (D7).
13. **Logs carry IDs, outcomes, timings and error codes only** — no message text or personal details (D17).
14. **Templates escape everything;** model output is never inserted as raw HTML (D12, D20).
15. **Evals are sacred:** never edit labels, expected answers or metric definitions to raise a number; the held-out split is never used for tuning; new cases go to `dev/` (D4, D16).
16. **Money is integers** end to end: customer money (fees, invoices) in cents; our model spend in micro-dollars (`cost_usd_micros`), because one turn costs a fraction of a cent.
17. **Secrets only from env**, listed in `.env.example`; never printed or logged.

## 4. Data model

**Meridian tables (given, schema unchanged):** `customers`, `technicians`, `appointment_slots`, `appointments`, `invoices` — see `data/meridian_dataset/scheduling_db/SCHEMA.md`.

**App tables (added in the same file):**

| Table | Key columns | Purpose |
|---|---|---|
| `conversations` | id, started_at, ended_at, visitor_key, customer_id (nullable), outcome, outcome_reason, rating, turns, cost_usd_micros, models_used | One row per chat (D15) |
| `turn_events` | conversation_id, seq, role, text, worker, tool_calls (JSON), sections (JSON), check_result, latency_ms, model, tokens_in, tokens_out, cost_usd_micros | Steps for the detail page (D15) |
| `handoff_tickets` | id, conversation_id, reason, urgency, customer_id, verified, summary, tried, wants, callback_window, status, resolution_code, created_at | D11, D16 |
| `fee_reviews` | id, conversation_id, appointment_id, change, amount_cents, consent_text, consent_method, consent_at, status, notice_text | D10 |
| `flags` | id, conversation_id, kind, created_at, review_tag, correct_answer, reviewed_by, added_as_test, test_case (JSON); unique per (conversation_id, kind) | D16 |
| `usage_daily` | date, cost_usd_micros, conversations, limit_hits | D14 spend cap |
| `rate_events` | visitor_key, kind, at | D14 per-visitor limits |

Canary customers (D20) are inserted into the runtime DB copy at first boot, never into the dataset file. Schema changes are numbered migrations applied above `PRAGMA user_version` (`db/__init__.py`); a released step is never edited.

## 5. Agent flow (one message)

```
caller message
  → limits (D14)                     reject → "unavailable" / limit message (no model call)
  → code gate (D8.2, D11)            safety → script + emergency ticket; crisis → 988 reply + urgent ticket;
                                     callback window / closed chat → fixed reply; person or manager → handoff;
                                     fee consent pending → button or typed "I agree" (no model)
  → supervisor (Haiku)               route → kb | scheduling | handoff (with reason)
       kb worker: search_kb → answer with citations
       scheduling worker: verify_identity → find_slots / book_slot / reschedule_visit / cancel_visit / get_appointments …
       handoff (code): ticket + caller message
  → verify_answer (D19)              model-written replies; fail → retry once → handoff
  → reply + record turn (D15)
```

**Shared state** (`agent/state.py` `Conversation`): history, identity session (verified customer, failures), offered slots and the turn they were shown, read-back awaiting a yes, pending cancel, pending fee, what was tried, tool results so far, closed flag, callback ticket. Per turn (`Turn`): route, model calls, tool calls, sections, citations, changes, check failures, ticket.

**Tools** (`agent/tools.py`): supervisor: `route(worker, reason?)`. KB worker: `search_kb(query)`, `answer(text, citations)`, `request_handoff(reason, summary, wants)`. Scheduling worker: `search_kb(query)`, `verify_identity(name, account_number?|phone?|address?)`, `find_slots(service_type, date?, window?, earlier?)`, `book_slot(option, issue)`, `get_appointments()`, `reschedule_visit(appointment_id, option)`, `cancel_visit(appointment_id)`, `get_invoices()`, `get_membership()`, `quick_checks(problem)`, `request_handoff(reason, summary, wants)`. Slots are picked by offered option number, not slot ID. There is no fee-consent tool: consent is taken in the code gate (D10.8). Customer ID is never a tool argument (Rule 6).

## 6. Key flows

- **KB answer:** search → top 4 sections → answer with quoted citations → D19 checks A + B → reply. No supporting section → handoff "no source".
- **Booking:** identity → (troubleshooting offer) → find slots in zone/service → offer → classify reply → if *agrees* to exactly one offered option → atomic book (if still bookable) → confirm with date, window, service, tech name. Not a clear pick → code sends a "Just to confirm" read-back; cancels are confirmed the same way.
- **Change inside 24 h:** code-written fee notice (amount read from the fee schedule) → button "I agree to the $49 fee" or typed "I agree" → change + fee-review item in one transaction; anything else → notice again, and a second non-consent → no change, handoff `late_change_fee_declined`.
- **Handoff:** what / why / next → ticket card in chat → ticket in staff queue.
- **Spend cap reached:** chat shows "unavailable" message; dashboard shows spend against the cap; reset at midnight Central (real date, `clock.ops_date`).

## 7. Seams and stubs

| Seam | Real | Test double |
|---|---|---|
| Model adapter | Anthropic / OpenAI | Fake adapter with scripted replies and tool calls; failure modes (timeout, 429, malformed tool call) |
| Embeddings | OpenAI | Deterministic fake vectors |
| Clock | Frozen `APP_TODAY`, real Central time of day | Injected date / patched `clock.now` |
| DB | Runtime copy on volume | Temp copy per test |
| Model provider check (`/health?deep=1`) | Anthropic list-models call | Injected `model_check` |
| Jev | Jev API | Fallback fast LLM |

## 8. Testing strategy

- **Unit / integration (CI, no network):** scheduling rules and invariants (Hypothesis: no slot both `open` and referenced by a `scheduled` appointment), identity, verify_answer, limits, safety check, handoff payloads, web hardening headers, template escaping — all with the fake model and temp DBs.
- **Eval smoke (CI, fake model):** harnesses run end to end and produce result files.
- **Live evals (opt-in, owner's Mac, run by hand with `.env`):** retrieval, conversations (dev / held-out / final; routing accuracy and grader sentinels are part of this run), red-team (against the agent in-process with temp DB copies), Sonnet vs Haiku. Results committed under `evals/*/results/`.
- **Live checks after deploy:** `scripts/live_checks.sh` (headers, health, docs off, forwarded IP; no model calls); hourly `/health?deep=1` and daily scripted conversations from GitLab schedules (D15.6).

## 9. Known limitations

See the **Known limitations** table at the bottom of [`decisions.md`](decisions.md) (one row per decision that introduced one).
