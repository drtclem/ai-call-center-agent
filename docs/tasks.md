# Tasks

_Owner: Taylor Clements. Built by Cursor under `.cursor/rules/workflow.mdc`. Kick-off: `Do task T<n> from docs/tasks.md.`_
_Last updated: 2026-10-18_

## Conventions (read before every task)

- **🔒 = locked task:** Cursor stops at a green MR and reports "T<n> ready for review"; Claude reviews and merges (D1). All other tasks auto-merge on a green pipeline.
- **[OWNER]** = a step only Taylor can do (accounts, live checks from his terminal). **[CLAUDE]** = a step Claude does before or after the task.
- **Blocking rules** are in `docs/architecture.md` §3. The reviewer treats a violation as Blocking.
- **Tests:** CI runs with the fake model and temp DB copies, no network. Live model calls only with `LIVE=1` on the owner's Mac.
- **Done when** checkboxes reference requirement IDs (`R-##`) in `docs/requirements.md`; the requirements-tracer confirms each has a real test.
- **Report every AI correction** (wrong API guess, invented flag, self-correction) at the end of the task for `docs/ai-log.md`.
- Use Cursor's **local agent mode**; if you use a cloud agent, commit everything first (it stashes uncommitted changes).

---

## Phase 0 — Foundation

### T0. Scaffold, tooling, CI
Create the Python project at the repo root per `docs/architecture.md` §2 (empty packages are fine). Steps, in order:
1. Check current versions (official sources) of Python 3.14, uv, FastAPI, uvicorn, Jinja2, LangGraph, the Anthropic and OpenAI SDKs, pytest, pytest-asyncio, Hypothesis, ruff, pyright, httpx; pin them in `pyproject.toml`; `uv lock`. Justify each dependency in the report.
2. `pyproject.toml`: ruff (lint + format), pyright strict on `src` and `tests`, pytest config.
3. `src/meridian/config.py` reading env (no values printed); `src/meridian/clock.py` returning `APP_TODAY` (default `2026-10-16`).
4. A trivial FastAPI app with `GET /health` returning `{"status": "ok"}`, `/docs` / `/redoc` / `/openapi.json` disabled when `ENV=production`.
5. `.gitlab-ci.yml`: stages lint (ruff check, ruff format --check, pyright), test (pytest), security (gitleaks). Mirror in `scripts/ci.sh`. Same runner setup as boostlingo (Docker executor).
6. `Dockerfile` (uv-based, non-root user, runs uvicorn).
7. Update `AGENTS.md` "Commands" and the README "Run it" section.

**Done when**
- [ ] `scripts/ci.sh` passes locally; branch pipeline green on GitLab.
- [ ] `GET /health` test passes; test proves `/docs` returns 404 with `ENV=production` (R-34).
- [ ] Test proves `clock.today()` returns `APP_TODAY` (R-21).
- [ ] `.env.example` lists every variable used.

### T1. Spike S1: Jev access [OWNER]
Write `discovery/jev_check.py`: classify 5 transcripts into a draft 6-bucket list with Jev and print only labels and scores (no secrets). Find how Jev is called today (API / gateway / SDK), price and rate limits, from official docs.
**[OWNER]** run it from the terminal and paste the output to Claude.
**Done when**
- [ ] Script runs, or the report says exactly why Jev isn't usable → fallback labeler noted. **[CLAUDE]** updates D2 (provisional → confirmed).

## Phase 1 — Discovery (decides D3, D4)

### T2. Discovery pass 1 and bucket list
`discovery/pass1.py`: Haiku writes one line per call (why they called, resolved or escalated, account lookup needed, second reason), cached to `discovery/cache/`. `discovery/buckets.py`: Opus groups the 420 lines into candidate buckets with definitions and 2–3 example call IDs each → `discovery/buckets_draft.md`. Embedding + clustering cross-check over pass-1 lines → clusters the bucket list doesn't explain, in the draft.
**Done when**
- [ ] 420/420 calls have a pass-1 line; re-running uses the cache (no API calls) (R-01).
- [ ] `buckets_draft.md` committed. **[CLAUDE]** reviews it against raw calls; **[OWNER]** signs off the list.

### T3. Discovery pass 2, review, report 🔒
`discovery/label.py`: labeler 1 (Opus) and labeler 2 (Jev or fallback) assign primary + optional secondary bucket to every call, blind to each other; disagreements get a suggested label (OpenAI flagship) with a one-line reason. A local review page (`uv run python -m discovery.review`) lists disagreements with transcript, both labels and the suggestion; confirm or override writes to `discovery/labels.csv` (call_id, labels, final, decided_by). `discovery/report.py` writes `discovery/report.md`: volume ranking (primary only; secondary alongside), minutes per bucket from `duration_sec`, agreement %, Cohen's kappa, confusion pairs, owner overrides, unbookable-case counts (north plumbing, Austin estimates).
_Status 2026-10-19: done. The labelers agreed on 420/420, so the disagreement review page was not built (nothing to review). If a re-run produces disagreements, the owner's picks go in `discovery/owner_overrides.json` (`{"call_id": "bucket_id"}`) and `discovery.report` uses them._

**Done when**
- [ ] Every call has a final label; every disagreement shows `decided_by: owner` (R-01, R-02).
- [ ] Report numbers recomputed from `labels.csv` by a test (R-02, R-03).
- [ ] **[OWNER]** reviews disagreements. **[CLAUDE]** brings D3 (scope) and D4 (test set) to the owner with the numbers; Phase 3+ tasks marked "blocked on D3/D4" unblock.

## Phase 2 — Knowledge base

### T4. KB manifest, conversions, issues report
`kb/manifest.toml` for all 17 files (status, effective date, audience, format, supersedes) per D5. Converters: PDF → Markdown table; PNG → text via a vision model, written to `kb/converted/` with source file noted (**[CLAUDE]** verifies against the image before merge); CSV → one line per item with column names. Build step fails on any KB file not in the manifest. Write `docs/kb-issues-for-meridian.md` (I3–I6, I13–I15).
**Done when**
- [ ] Test: unlisted file → build fails (R-05).
- [ ] Test: no `superseded`, `stub` or `agent_only` doc reaches the index (R-06).
- [ ] Converted holiday hours and fee table committed and verified (R-07).

### T5. Retrieval and retrieval eval 🔒
**[CLAUDE]** first commits `evals/retrieval/questions.toml` (~60 questions with expected doc(s), incl. traps).
Chunk current docs by heading (doc name, heading, effective date on each piece; table pieces repeat the header row). Keyword search + embedding search (`text-embedding-3-small`), rankings merged; `search_kb(query) → top 4`. Index built at startup. Eval runner writes `evals/retrieval/results/latest.md` (+ `.json`; git history keeps earlier runs): answering section in top 3 (count, %), misses with reasons, old/internal docs returned (must be 0).
**Done when**
- [ ] Unit tests with fake embeddings for chunking and merge (R-08).
- [ ] Live eval result committed (R-09); old/internal returned = 0 (R-06).

## Phase 3 — Core agent

### T6. Database layer and clock
Runtime DB path from env; first boot copies the dataset DB if the volume file is missing; app-table migrations (architecture §4); canary customers inserted into the runtime copy only. Zone map from `SCHEMA.md`.
**Done when**
- [ ] Test: first boot copies; second boot doesn't overwrite (R-31).
- [ ] Test: dataset file unchanged after a full test run (checksum) (R-31).

### T7. Caller identity 🔒
`identity/`: verify per D9 (name forgiving; numbers exact after removing spaces/dashes; address normalized; duplicates resolved by the second factor; 2 failures → handoff); session lock used by every account tool.
**Done when**
- [ ] Tests: each factor; "Robert Patel" ×3; wrong second factor message reveals nothing; 2 failures → handoff; locked session can't read MHC-10001 when verified as MHC-10002 (R-10, R-11, R-39).

### T8. Scheduling tools 🔒
Per D10: find slots (zone, service type, member-reserved, Tue/Thu window, from tomorrow, nearest 3, "earlier"); book / reschedule / cancel / confirm with atomic conditional writes; warranty flag (< 2 years since install); inside-24 h fee path with consent record and fee-review item; unbookable zone/service → handoff reason; urgent → handoff.
**Done when**
- [ ] Tests on temp DB: every rule above; two concurrent bookings of one slot → exactly one wins; property test of the slot/appointment invariant (R-12–R-17).
- [ ] Linda Foster reschedule example from the PRD passes end to end at tool level (R-14).

### T9. Model adapter
`agent/llm.py`: Anthropic (Sonnet, Haiku), OpenAI backup, embeddings; timeouts; one retry then backup; usage → cost in cents; fake adapter for tests.
**Done when**
- [ ] Tests with fakes: timeout → retry → backup → both fail → error the graph turns into a polite handoff; cost computed (R-24, R-25).

### T10. Agent graph and escalation 🔒
LangGraph per D8: limits hook, safety check (code), supervisor (Haiku), KB worker, scheduling worker, handoff (code), shared state. Replies: confirmation classification (agrees / declines / different / unclear) with one read-back on unclear; troubleshooting offer; handoff wording what/why/next and the ticket (D11); "talk to a human" immediate; greeting "Hi, I'm Meridian's virtual assistant"; no tool or workflow disclosure.
**Done when**
- [ ] Tests (fake model): safety phrases → script on the first message without a model call (R-18); each D11 trigger → correct reason and complete ticket (R-19, R-20); real "yes" phrasings book and trap phrasings don't (R-15); topic-hop keeps verified identity and pending offer (R-22).

### T11. Answer verification 🔒
`agent/verify_answer.py`: check A (numbers, dates, windows, phones must appear in this turn's sections or tool results, after normalizing formats), check B (cited section retrieved this turn; quote verbatim), canary instruction phrase blocked; retry with the failure fed back; second failure → handoff; failures recorded.
**Done when**
- [ ] Tests: "$99 tune-up" blocked; correct answer passes; fake quote blocked; canary phrase blocked; retry then handoff (R-23).

## Phase 4 — Web, limits, monitoring

### T12. Chat page
Templates + HTMX + one stylesheet (tokens, light/dark, phone-friendly, visible focus). Greeting, demo-date banner, fictional-data notice, message list, fee consent button ("I agree to the $49 fee") with typed fallback, handoff ticket card, 👍/👎 after contained conversations, "unavailable" state.
**Done when**
- [ ] Tests: model output with `<script>` renders escaped (R-35); consent only via button or "I agree" (R-16).
- [ ] **[CLAUDE]** looks at the page (screenshot) before merge.

### T13. Public-URL limits and web hardening 🔒
`limits.py` per D14 (message length, per-visitor rates, turns, conversations, bookings, $15/day cap from recorded cost, midnight Central reset). Hardening per D20: cookie flags and expiry, CSP header, no version banners, docs endpoints off, no URL fetch.
**Identity guessing (T7 review):** the 2-failure limit is per conversation, so record each identity NO_MATCH as a `rate_events` row (`identity_fail`) keyed on IP alone (not the cookie), cap it per IP per day (e.g. 5), after which identity always hands off without running the matcher; test that a fresh conversation from the same IP inherits the block.
**Done when**
- [ ] Tests: each limit trips with the right message and is recorded; cap → "unavailable" (R-32, R-33); headers and cookie flags asserted (R-34).

### T14. Monitoring, dashboard, staff pages
Event recording per D15 (no message text in logs, D17); outcomes incl. abandoned (30 min) and 👍/👎; staff password; dashboard tiles, 14-day trend, recent problems, handoff and fee-review queues; conversation detail page; masked contact details in lists; reset demo data and delete conversation logs actions.
**Done when**
- [x] Tests: outcomes computed correctly incl. abandoned ≠ contained (R-28); log lines contain no message text or phone/email (R-37); staff pages need the password (R-38); metrics queries match a seeded fixture (R-29).

## Phase 5 — Evals (blocked on D3/D4 where marked)

### T15. Conversation eval harness 🔒
Per D4: build dev (60) and held-out (150) cases from transcripts (seeded, disjoint, mirroring bucket shares) with identity swapped onto matching DB customers and answer keys from the KB/DB; simulated caller (Haiku); grader with sentinels (D20), metrics: containment (with fee follow-up, abandoned and 👎 separate), accuracy on handled, escalation rate on the rest, grounded rate (D19 check C), routing accuracy over discovery labels (D8), Sonnet vs Haiku (D7). One command reproduces; results to `evals/conversations/results/`.
**Done when**
- [x] Smoke run with fake model in CI (R-26).
- [x] Live results committed for Sonnet and Haiku on the held-out split (R-04, R-26, R-27).
- [x] Final reported run on all 360 non-dev calls (T21).
- [x] Sentinel failure marks a run untrusted (R-30).

### T16. Red-team set and canary customers 🔒
`evals/redteam/cases.yaml` (~30, every D20 row, OWASP-tagged) with leetspeak, base64 and crescendo transforms; canary leak detector; runner against the app (fake model in CI for the code-limit cases; live on the Mac and against the URL).
**Done when**
- [x] 100% pass live; any canary in an unverified session fails the run (R-39, R-40).

## Phase 6 — Ship and operate

### T17. Deploy to Railway 🔒
**[OWNER]** `railway init` from `~/Projects/aea_callcenter`, create a project token, add it as a masked GitLab CI variable `RAILWAY_TOKEN`; add model keys and staff password as Railway variables.
Railway service + volume at the runtime DB path; CI deploy job on green `main`; spike S2 checks: HTTPS on the Railway domain, volume survives a redeploy, backup options.
**Done when**
- [x] Public URL serves the chat over HTTPS (R-36).
- [x] A booking survives a redeploy (R-31). **[CLAUDE]** updates D13 (provisional → confirmed).
- [x] Logs check (T12 review, D17): Starlette re-raises after our error handler, so uvicorn logs tracebacks with exception messages; confirm none can carry caller text in production (or configure uvicorn's error logger to drop messages).
- [x] Image check (T6 review): `docker build` then confirm the dataset DB, `kb/manifest.toml`, `kb/converted/` and `kb/index/` are in the image (Dockerfile copies only `src` and the dataset DB today), the app user can create `DB_PATH` on the Railway volume (volumes may mount root-owned), and `DB_PATH` is set (the app refuses to start in production without it).
- [x] Forwarded-IP check (T13 review S1, D14): from outside, send a request with a fake `X-Forwarded-For: 9.9.9.9` and confirm the visitor key is the real IP's hash. If Railway appends instead of replacing, change the trusted entry and record the result in D14.
- [x] Header check (T13 review N8): `curl -I` the live URL shows HSTS, CSP and no `server` header.

### T18. Alerts
GitLab scheduled pipelines: hourly `/health` (app, DB, model provider reachable); daily 2 scripted live conversations (KB question; booking check on a canary customer). Failed pipeline emails the owner.
**Done when**
- [x] A deliberately failing health check produces a failed scheduled pipeline (R-30b). **[OWNER]** confirms the email arrived.

### T19. Feedback loop
Auto-flags (D16), review page with tags and correct answer, `reviewed_by`, add-as-test into `evals/conversations/dev/` only, handoff resolution codes and a count on the dashboard.
**Done when**
- [x] Tests: each flag kind created; add-as-test writes only to `dev/`; held-out untouched (R-41, R-42).

### T20. Reviewer docs
`docs/threat-model.md` (OWASP LLM Top 10 2025 + classic, blast-radius ranking, coverage table with reasons); finalize `docs/kb-issues-for-meridian.md`; README: one-command local setup, the URL, seeded test customers, the numbers with links to result files, known limitations.
**Done when**
- [x] Every number in the README links to a committed result file (R-43).

### T21. Final eval run and writeup
Full live run of every eval on the release commit; README numbers updated; **[CLAUDE]** drafts the 3–4 minute video outline (discovery, scope, retrieval, escalation, numbers) from `decisions.md` and the results.
**Done when**
- [x] requirements-tracer shows no `Gap` rows; all results dated on the release commit (R-44). The final, red-team and retrieval runs are on `30e6f2f`; the dev and Haiku comparison runs are on `5e59c16` and labeled as such in the README.
