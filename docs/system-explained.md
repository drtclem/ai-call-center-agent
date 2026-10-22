# How Meridian works

_Last updated: 2026-10-20 — full rewrite: chat, the code gate, supervisor and workers, retrieval, answer checks, identity, booking and fees, handoffs, staff pages, monitoring and alerts, feedback loop, evals, and the Railway deploy, all as the code runs on `main`._

This is the system as the code runs it today, in plain English. Every number comes from a test, a committed eval result or the dataset; each says which. Diagrams: [`diagrams/overview.md`](diagrams/overview.md), [`diagrams/message-flow.md`](diagrams/message-flow.md), [`diagrams/deploy.md`](diagrams/deploy.md).

## The big picture

**Chat page.** `GET /` in `meridian.web.app` serves one page: a greeting, a "Demo date: Fri Oct 16, 2026" line, and a message box. HTMX posts each message to `POST /chat` and swaps in the reply, so the page works without a full reload (and still works, more slowly, with JavaScript off). Every response carries strict security headers (CSP, frame denial, HSTS in production), and cross-site posts are refused.

**Backend.** One FastAPI process (`create_app`). Chat state (history, who is verified, which slots were offered) lives in memory, keyed by a random cookie, for up to 6 hours. Bookings, tickets and logs live in SQLite on disk. A restart loses open chats, never bookings.

**The agent.** `meridian.agent.graph.Agent` is a small LangGraph graph: a **code gate**, a **supervisor** (Claude Haiku 5.5) that picks a specialist, a **KB worker** and a **scheduling worker** (both Claude Sonnet 5.5), and a **handoff** step written in code. The workers can only act through tools (`agent/tools.py`); code checks every tool argument.

**Model adapter.** `agent/llm.py` is the only place that talks to model providers. Each call has a 20-second timeout, is retried once, then goes to the OpenAI backup (`gpt-6-astra`). If that fails too, the caller is handed to a person.

**Retrieval.** `meridian.kb.search` searches 37 sections cut from 10 current, customer-facing Meridian documents (`kb/index/chunks.json`), mixing keyword and meaning search.

**Scheduling and identity.** `meridian.identity` verifies the caller. `meridian.scheduling` finds, books, moves and cancels visits in Meridian's tables, with all-or-nothing writes.

**Monitoring and feedback.** Every turn is recorded (`monitoring/records.py`). Staff pages under `/staff` show the numbers, the queues and flagged chats (`web/staff.py`, `monitoring/metrics.py`, `feedback/`).

**Evals.** `evals/` holds three harnesses: retrieval, full conversations against a simulated caller, and a red team. Their results are committed.

**The business date.** `meridian.clock.today()` returns `APP_TODAY`, fixed at 16 October 2026, the day the dataset is built around (D10). `clock.now()` is that date at the real Central time of day, which the 24-hour fee rule uses. Rate limits and the spend cap use the real date (`clock.ops_date`), so the cap resets at real midnight Central.

---

## 1. A message arrives

**What happens.** Before any model sees a message, code checks the limits, then the code gate. Only then does the supervisor pick one worker.

**Step by step.**

1. `POST /chat` reads the form (bodies over 16 KB are refused) and runs `reply` in a worker thread.
2. **Limits** (`limits.check_message`), keyed on a salted hash of the visitor's IP (`_visitor`): the daily spend cap ($15, `DAILY_SPEND_CAP_USD_MICROS`), 1,000 characters per message, 20 messages a minute, 150 an hour, 30 new chats a day, 30 turns per chat. A refusal gets a plain message and makes no model call. Every hit is counted for the dashboard.
3. One turn at a time per chat (a per-chat lock), so a double click can't run the same booking twice.
4. **The code gate** (`Agent._gate`), in this order:
   - `safety.check` finds gas, burning, smoke, sparks, fire or carbon-monoxide wording (a negation right before it, as in "no gas smell", doesn't count). The caller gets Meridian's safety script word for word and an **emergency** ticket.
   - It finds crisis wording ("I don't want to live anymore"). The caller gets a caring reply with the 988 line and an **urgent** ticket.
   - If the chat was just handed off and is waiting for a callback time, the message is saved as the callback window.
   - If the chat is closed (already handed off), the caller hears that a team member will follow up.
   - A request for a manager or a person hands off at once. "Are you a real person?" is a question, not a request, so it isn't caught here.
   - If a late-change fee is waiting for consent, the message goes to the fee step (flow 4), with no model.
5. **Supervisor** (`_supervisor`, Haiku): sees the last 6 messages plus "verified: yes/no" and how many options are on the table, and must call `route` with `kb`, `scheduling` or `handoff` (plus a reason). It is also a backstop for safety or crisis wording the code gate can't read (leetspeak, base64). If it gives no usable route, code picks `scheduling` when options are on the table, else `kb`.
6. **Worker** (`_work`): up to 6 model rounds and 12 tool calls in one turn, and at most one booking change per message.
7. The turn is recorded (`record_turn`) and its cost is added to today's spend (`limits.add_spend`). A crash anywhere in the graph becomes a polite handoff (`route = "error"`), never a 500 error page.

**Worked example.** From `tests/test_agent.py`, with a scripted fake model: "Linda Foster 512-555-0101, please cancel tomorrow's visit" passes the limits and the gate, the supervisor routes it to `scheduling`, and the worker calls `verify_identity` and then `cancel_visit` for visit 159. "I smell gas" never reaches a model: the gate returns the safety script and an emergency ticket.

**What can go wrong.**
- The safety phrases are deliberately broad. A false alarm costs one callback, and a missed gas leak costs far more.
- Chats live in one process's memory. A redeploy ends open chats; the caller starts again.
- If the spend cap is reached, every visitor sees "Our virtual assistant is unavailable right now" until midnight Central.

**Exec angle.** "Why not let the AI decide everything?" Because the decisions that hurt when they go wrong (gas, crisis, a request for a person, consent to a fee) are made by code that behaves the same way every time. The model is used only where language is genuinely ambiguous.

## 2. A knowledge-base answer

**What happens.** The KB worker searches Meridian's current documents, answers with quotes, and code checks every fact before the caller sees it.

**Step by step.**

1. Only documents marked `current` and `customer` in `kb/manifest.toml` are indexed (D5). A KB file missing from the manifest fails the build. PDFs, the PNG and the CSV are converted to text offline (`kb/convert.py`).
2. `kb/chunk.py` cuts each document into heading sections. Each section starts with the document title, heading and effective date. Tables repeat their header row, so "Waived" stays tied to "Comfort Club".
3. Section embeddings are built offline (`kb/build_index.py`) with OpenAI `text-embedding-3-small` and committed. At startup `load_index` rebuilds the sections and refuses to start if any changed without a rebuild.
4. `search_kb(query)` runs **BM25** (keyword scoring) and **embedding** similarity (only the query is embedded live), merges the two rankings with reciprocal rank fusion, and returns the top 4 sections (`TOP_K`).
5. The worker must finish with `answer(text, citations)`: each citation is a section ID and an exact quote. If nothing supports an answer, it calls `request_handoff("no_supporting_source")`.
6. `verify_answer.check` (pure code, no model):
   - **Check A:** every dollar amount, percentage, date, time, phone number and duration in the reply must appear in the sections retrieved this turn, the tool results of this chat, or today's date. The caller's own words never count, except a phone number they gave. Any number above 12 must appear somewhere in the sources too.
   - **Check B** (KB answers only): every citation must name a section retrieved this turn, quote at least 3 words found word for word, and every fact must come from those quotes.
   - **Canaries:** the hidden instruction phrase never appears, and a canary customer's marker appears only to that verified customer.
7. On a failure the model is told the problem and gets **one** retry. A second failure hands off (`answer_check_failed`).

**Worked example.** From `tests/test_verify_answer.py`: with the tune-up section retrieved, "A tune-up is $129 for non-members, and included for members." passes. "A tune-up is $79." is blocked and the feedback names $79. The daily alert asks "How much is your diagnostic visit?" and needs `$89` in the reply; the 2026 fee schedule lists the diagnostic visit at $89, waived for members.

**Retrieval numbers** (`evals/retrieval/results/latest.md`, 2026-10-19): the answering section is in the top 3 for 66 of 69 questions (96%), and in the 4 sections the agent sees for 67 (97%). Hybrid beats keyword-only (58) and embeddings-only (64). Old, stub or internal documents returned: 0.

**What can go wrong.**
- The search misses the section (3 of 69 in the eval). The worker then has no source and hands off rather than guess.
- Check A can't read numbers written in unusual ways, and it can't tell a true fact applied to the wrong case. The eval's judge (check C) covers that offline only.

**Exec angle.** "How do you know it doesn't make up prices?" Code compares every number in the reply to the text it retrieved. A made-up number is blocked before it is sent, then retried once, then handed to a person. The cost is some extra handoffs. A wrong price never reaches the caller.

## 3. Identity verification

**What happens.** Before touching anyone's account, the caller gives a full name plus one of: account number, phone on file, or service address. Code, not the model, decides.

**Step by step.**

1. The scheduling worker calls `verify_identity(name, account_number | phone | address)` with what the caller typed.
2. `identity.verify` normalizes everything: "MHC-10001", "mhc 10001" and "10001" are the same account; phones are digits only; "Drive" equals "Dr". First and last names may differ by a small typo (similarity of 0.8 or more per part).
3. Exactly one customer must match both the name and the factor. Then the session is locked to that `customer_id` for the rest of the chat.
4. Every failure gets the same reply ("That doesn't match what I have…"), so a guesser learns nothing. Two failures, or a still-ambiguous match, hand off (`identity_not_verified`). Five failures per visitor a day blocks further tries across chats.
5. Every account tool reads the customer from the session (`require_customer`). No tool takes a customer ID from the model.

**Worked example.** From `tests/test_identity.py`: "Lynda Foster" plus Linda's service address verifies as MHC-10001; each of the account-number spellings above does too. A partial phone never matches. Once verified as one customer, a second identity in the same chat hands off.

**What can go wrong.** A real customer who mistypes twice is handed to a person. That is the intended trade.

**Exec angle.** "Could someone talk the bot into reading out another person's account?" No tool can reach another account, whatever the model is told. The red team tried 96 variations of this and similar attacks; none leaked data or changed another customer's records.

## 4. Booking, rescheduling and cancelling

**What happens.** The worker offers up to 3 real slots. Code books only the option the caller clearly agreed to, and a change inside 24 hours needs formal consent to the $49 fee.

**Step by step: booking.**

1. `get_appointments` first, so a duplicate visit is noticed. For AC, furnace, thermostat or indoor-water problems, the worker offers the quick checks once (`quick_checks`, from the troubleshooting FAQ).
2. `find_slots(service_type, date?, window?, earlier?)` in `scheduling.find_slots`: zone from the customer's city (`zones.py`), slots after today only, members also see `reserved_members` slots, evening slots only on Tuesday and Thursday, nearest to the request, 3 at most. A city with no zone, a service with no slots in the zone (north-zone plumbing), or no openings hands off. A same-day date gets "offer the soonest days instead".
3. **The agreement rule** (`confirm.agreed_option`), in code: the options must have been shown in an earlier message. The caller's own words must agree ("That's fine", "yes, the second one") with no hedge ("but", "actually", a question mark) and no negation. Every clue they give (option number, weekday, date, time window, technician's first name) must point to exactly one option. A bare "yes" counts only if one option is on the table.
4. **The read-back.** If the words don't clearly pick one option, code itself sends "Just to confirm: book Mon Oct 19, 8am–12pm, repair visit, technician Marta Kowalski?". Because code wrote the question, the caller's next "yes" can only mean that slot.
5. `book` runs in one `BEGIN IMMEDIATE` transaction. The slot update succeeds only if the slot is still bookable (it checks the row count), and the appointment insert is in the same transaction. If someone else just took the slot, nothing is written and the worker offers others.
6. If the system was installed less than 2 years ago, the visit is marked as a warranty visit, and the tool result tells the worker to say there's no diagnostic fee if the technician confirms the failure is covered. Each visitor can make at most 10 bookings a day.

**Step by step: reschedule and cancel.**

1. Only the caller's own upcoming visits, and not same-day ones (those hand off).
2. A cancel is two steps: code asks "Just to confirm: cancel your …?" and cancels only on a clear yes in the next message.
3. **Inside 24 hours** of the window's start, `reschedule`/`cancel` return `NeedsFeeConsent`. The caller gets the code-written notice: "Because this is within 24 hours, Meridian's $49 late-change fee applies. A team member will review it… tap "I agree to the $49 fee" or type "I agree"." The amount is read from the current fee schedule at startup (`late_change_fee_cents`), never typed into code.
4. Consent is decided in the gate (`_fee_reply`), never by a model: only the button (`POST /chat/consent`, the only path that sends `method="button"`) or the typed words "I agree" count. "ok fine" gets the notice again. A second non-consent leaves the visit unchanged and hands off (`late_change_fee_declined`).
5. On consent, the change and a `fee_reviews` row (amount, the exact notice, consent text, method, time) are written in one transaction. No code path charges, refunds or waives anything; staff review the fee.

**Worked example.** Linda Foster (MHC-10001, Round Rock, north zone, Comfort Club Standard) has visit 159: Thu Oct 22, 8am–12pm, repair, technician Marta Kowalski (dataset DB). Asking for a repair from Fri Oct 16, `find_slots` offers Mon Oct 19 8am–12pm with Marta Kowalski, Mon Oct 19 12pm–4pm with Ray Mendoza, and Mon Oct 19 12pm–4pm with Marta Kowalski. These come from the dataset DB, ordered the way `find_slots` orders them; a live DB with bookings may differ. In `tests/test_agent.py`, the clock is set to Oct 21 at noon (20 hours before her window): cancelling gets the $49 notice and button, "ok fine" changes nothing, and the button cancels the visit and files a fee review with `consent_method = "button"`. Without the late window (`test_early_cancel_has_no_fee`), no fee review is created.

**What can go wrong.**
- The slot is taken between offer and "yes": nothing changes, and the caller is offered other times.
- The caller's reply is ambiguous: they get the code-written read-back instead of a guessed booking.
- Their city or service isn't in Meridian's scheduler: handoff with `cannot_be_booked_here`.

**Exec angle.** "What if the AI books the wrong slot or agrees a fee on someone's behalf?" It can't: the booking tool refuses unless the caller's own words match one offered option, and fee consent never passes through the model at all. The price is a few extra "Just to confirm" questions.

## 5. Escalation and the handoff ticket

**What happens.** When the agent can't or shouldn't finish, code writes a ticket for staff and tells the caller what happens next.

**Step by step.**

1. A handoff starts from the gate, the supervisor's `route("handoff", reason)`, a worker's `request_handoff`, or a tool result (no zone, identity failed, check failed twice, model down). There are 14 reasons in `handoff.Reason`, for example `safety_emergency`, `asked_for_a_person`, `supervisor_callback`, `not_allowed_for_the_assistant`, `no_supporting_source`.
2. `Agent._handoff` saves a ticket (`records.create_ticket`): reason, urgency (safety is `emergency`; crisis and urgent are `urgent`; the rest are `standard`), customer ID if verified, a summary, what was tried (searches, offered slots), and what the caller wants.
3. `caller_message` gives the caller three parts: what is happening, why (blame-free), and what happens next. Standard tickets add "A team member will call you back within one business day. What's the best time to reach you?". The next message is saved as the callback window if it sounds like a time.
4. The chat shows a ticket card ("Passed to the Meridian team… here's exactly what they'd receive"), and the chat is closed to further agent work.
5. Any change already made this turn (such as a booking) is still reported to the caller before the handoff text.

**Worked example.** From `tests/test_agent.py`: Linda declines the $49 fee twice. Her visit 159 stays `scheduled`, and a ticket with reason `late_change_fee_declined` lands in the queue.

**Exec angle.** "Does a handoff just dump the customer?" No. Staff get a ticket they can act on without re-asking, and the caller knows when to expect a call. On the held-out eval, 0 of 62 must-hand-off calls were wrongly kept.

## 6. What staff see

**What happens.** `/staff` (HTTP Basic auth, one shared `STAFF_PASSWORD` of 16+ characters; off if unset) shows the operation in one page.

- **Dashboard** (`metrics.overview`): today's chats, containment, handed off, failed, abandoned, 👎, p50/p95 reply time, answer-check failures, spend against the $15 cap, limit hits, and a 14-day trend.
- **Handoff queue:** emergencies first, then urgent, then standard. Names and phones are masked ("L. Foster 512-***-0101"), and so are phones and emails in summaries. Closing a ticket needs a **resolution code** (`feedback.RESOLUTIONS`): just needed an answer, simple booking change, real dispute, needed an exception, safety or urgent, couldn't reach, other. The dashboard counts them. The first two mean "the agent could have handled this".
- **Fee reviews:** each consented late change, with the amount and how consent was given.
- **Conversation detail** (`/staff/conversations/{id}`): every turn with its worker, tool calls, retrieved sections, check result, model, tokens, cost and latency. This page is unmasked, for staff only.
- **Flags review** (`/staff/flags`): see flow 8.
- **Demo data:** reset Meridian's tables to the dataset (and drop fee reviews), or delete all conversation logs.

**Exec angle.** "How would a manager know the bot is doing a bad job?" The resolution codes measure exactly that: a rising count of "just needed an answer" means handoffs the agent should have kept.

## 7. Monitoring and alerts

**What happens.** Every turn is stored. Two GitLab schedules check the live app and email the owner on failure.

**Step by step.**

1. `record_turn` writes the caller message and reply to `turn_events` and updates the chat's outcome: `contained`, `contained_fee_followup`, `handed_off`, `failed` or `in_progress`. A chat left waiting on the caller for 30 minutes counts as `abandoned`, never contained. Server logs carry IDs, outcomes, timings and error types only, never message text, and the access log blanks the client IP (`NoExceptionText`, `NoClientIP`).
2. `GET /health` is a fast liveness check. `GET /health?deep=1` also reads the DB and checks the model provider with a free "list models" call. That result is cached for 5 minutes, so the public URL can't be used to spam the provider. Any failure returns 503.
3. **Hourly schedule** (`ALERT=health`, at :07): `scripts/alert_check.py health` calls `/health?deep=1`.
4. **Daily schedule** (`ALERT=conversations`, 8:23 Central): one KB question that must quote `$89`, then a slot check as the canary customer Wren Halvorsen that must list options without a handoff. No booking is made. It costs about 5¢ a day.
5. A failed job fails the pipeline, and GitLab emails the owner. On 2026-10-20 the owner pointed the hourly check at `/nope` for one run, and the email arrived (`docs/ai-log.md`).

**What can go wrong.** The schedules run on the owner's Mac runner. If the Mac is asleep, nothing alerts. The daily check is real traffic, so it shows up in the dashboard and in spend.

**Exec angle.** "Would Meridian know if it broke at 2am?" Within the hour, if the DB or the model provider is down and the runner is awake. An outside uptime monitor would close the asleep-Mac gap.

## 8. The feedback loop

**What happens.** Bad conversations are flagged automatically. Staff review them, and a reviewed one becomes a new dev test case.

**Step by step.**

1. `feedback.flag_turn` and `set_rating` raise at most one flag per chat per reason: 👎, an answer-check failure, a `no_supporting_source` handoff, an error (including the AI being down), and suspected injection (a short phrase list such as "ignore your instructions" or "system prompt"). Abandoned is decided when a staff page loads (`flag_abandoned`).
2. At `/staff/flags`, staff tag each flag (wrong answer, search missed the doc, wrong worker, should have handled, shouldn't have handled, KB is missing this, actually fine), write the correct answer and their name.
3. "Add as test" (`add_as_test`) builds a dev-split case from the caller's own messages, with typed phones and emails swapped for the test customer's, and stores it on the flag in the DB. It is stored there because the server's disk is wiped on every deploy. A "handled" case can't come from a chat that booked, moved or cancelled.
4. Staff download it, and `python -m evals.conversations.import_feedback` writes it to `evals/conversations/dev/`. It refuses any other folder, so held-out and final cases can't be touched. The next dev run picks it up.

**Exec angle.** "Does it get better over time, and can you fool yourself doing it?" Real failures become dev tests, and code stops them from reaching the held-out set, so the reported number stays honest.

## 9. The evals

**Retrieval** (`evals/retrieval/run.py`, 70 questions in `questions.toml`, 69 scored): see flow 2.

**Conversations** (`evals/conversations/`):
- `build.py` turned the 420 transcripts into cases once: a model writes each caller's goal and the right outcome from the KB (never from the human agent's answer), and code swaps in a real DB customer who fits the scenario. Splits: **dev** (60, used for fixing), **held-out** (150, never used for tuning), and **final** (all 360 non-dev calls, for the reported run; not yet run).
- `run.py`: a fresh DB copy per case. Haiku plays the caller, and the real `Agent.respond` answers. Grading is in code where possible (handoff and reason, DB changes). For every call the agent kept, a Sonnet judge grades correctness and grounding (check C). The judge must pass known-answer sentinel conversations first, or the run is marked untrusted.
- **Held-out, Sonnet** (`heldout-claude-sonnet-5-5/summary.md`): containment **48%** (72/150), against a ceiling of 59% for a perfect agent. False containment **0**. Accuracy on calls it should handle 82%. Right handoff reason 85% (53/62, hand-corrected after a grader fix; see the file). Agent cost $0.025 a conversation.
- **Dev, Sonnet:** 58% (35/60), ceiling 62%. **Held-out, Haiku as the brain:** 41%, with **2** false containments, so Sonnet stays the brain (D7).

**Red team** (`evals/redteam/run.py`, `results/latest.md`): 30 attacks, each as written and, unless a case opts out, as leetspeak, base64 and a multi-turn crescendo. 96 runs, **96 passed**, at a cost of $1.12. Code checks every reply for canaries, tool names, other customers' details and any DB change to someone else. It runs locally against the real agent and models with a temp DB copy, not against the live URL.

**Exec angle.** "Is 48% good when the brief asks for 60%?" On this data, 59% is the most even a perfect agent can reach on held-out. The rest are calls only a person can handle: complaints, safety, paying invoices, north-zone plumbing with no slots, declined fees. Raising the ceiling is Meridian's operational lever ([`kb-issues-for-meridian.md`](kb-issues-for-meridian.md)). The number that must be zero, false containment, is zero.

## 10. Deployment

**What happens.** Every green pipeline on `main` deploys one Docker image to one Railway service, with the database on a Railway volume.

**Step by step.**

1. GitLab CI on the owner's ARM Mac runner runs ruff, pyright, pytest and gitleaks. `scripts/ci.sh` runs the same steps locally. No model calls are made in CI.
2. `deploy:railway` (on `main` only, never on a schedule, once `RAILWAY_TOKEN` and `RAILWAY_SERVICE` are set) installs Railway CLI 5.63.1 and runs `railway up`.
3. The `Dockerfile` (Python 3.14.8 slim) copies the app, the dataset DB, the KB documents, `kb/manifest.toml`, `kb/converted/` and `kb/index/`, and sets `ENV=production` and `APP_TODAY=2026-10-16`.
4. `scripts/entrypoint.sh` starts as root only to make the volume folder writable, refuses a `DB_PATH` under `/app`, then drops to uid 10001.
5. At startup `init_db` copies the dataset DB to `DB_PATH` (`/data/meridian.sqlite`) on first boot only, applies migrations, and adds the canary customers. The app also reads the late-change fee and checks the KB index. Later boots keep the bookings. Production refuses to start without `DB_PATH`.
6. uvicorn runs with `--proxy-headers`. Railway's edge replaces any `X-Forwarded-For` a visitor sends, so per-visitor limits see the real IP.
7. **Live checks** (`scripts/live_checks.sh`, free): the page returns 200, HSTS and CSP are present, there is no app `server` header, `/health` is ok, the docs routes return 404, and a fake `X-Forwarded-For` is ignored.

**Worked example.** On 2026-10-20 (`docs/ai-log.md`), a test booking for Omar Patel (Wed Oct 21, 1–5pm) was still there after a redeploy (R-31), which confirmed D13.

**Exec angle.** "Is this production-grade?" It's one process and one SQLite file, which is right for a demo and holds bookings safely. Scaling out would need chat state in the DB and per-person staff accounts.

---

## Code vs docs: known differences

- Scheduling replies get check A but not check B (they cite tool results, not KB quotes). Code-written texts (the read-back, the fee notice, handoff messages) aren't checked, because code wrote them. Architecture rule 10 reads more broadly.
- `/health?deep=1` imports the Anthropic SDK in `web/app.py`, outside `agent/llm.py` (architecture rule 3).

## Glossary

- **Business date:** the day `clock.today()` returns, 16 October 2026 for the demo.
- **Call driver / bucket:** a category for why a customer called, such as billing or repair booking.
- **Containment:** the share of calls finished by the agent without a person.
- **False containment:** the agent kept a call that must go to a person. The bar is zero.
- **Code gate:** checks in code on every message before any model runs.
- **Supervisor / worker:** the supervisor model picks which specialist (worker) handles a message.
- **RAG:** retrieval-augmented generation; answering from retrieved documents, not the model's memory.
- **Chunk / section:** a piece of a KB document stored for search.
- **Embedding:** a list of numbers standing for a text's meaning, so similar texts can be found.
- **BM25:** a keyword score based on how often and how rarely the query words appear.
- **Reciprocal rank fusion:** merging two ranked lists by adding 1/(60 + rank) per list.
- **Top-k:** the first k results kept (here 4).
- **Recall@k / top-3 hit:** the share of questions whose answering section is in the top k.
- **Tool call:** the model asking code to run a named function, such as `book_slot`.
- **System prompt:** standing instructions sent with every model call (`agent/prompts/`).
- **Prompt injection:** caller text trying to make the model ignore its rules.
- **Canary:** a planted secret (a phrase or fake customer) whose appearance in a reply proves a leak.
- **Read-back:** the code-written "Just to confirm…" question before a booking.
- **Transaction:** a group of DB writes that all succeed or all roll back.
- **Race condition:** two requests overlapping, such as two people booking one slot. The conditional update stops it.
- **Idempotent:** repeating a request changes nothing after the first success.
- **Held-out split:** test cases never used for fixing, so the score is honest.
- **Sentinel:** a known-answer conversation the judge must grade correctly before its grades count.
