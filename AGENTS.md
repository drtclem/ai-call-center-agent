# AGENTS.md — how this project directs its coding agents

> **Since 2026-10-19 Claude builds** (the owner's Cursor credits ran out). The workflow, subagent roles and definition of done below are unchanged; the subagents run as fresh-context Claude agents using the same instruction files in `.cursor/agents/`. Claude works in its own clone, never in the owner's working folder, and merges after CI and the review agents pass (D1).

This file serves two readers: **coding agents** (Cursor reads it every session) and **reviewers** (who want to see how the work was directed). The binding rules are in `.cursor/rules/workflow.mdc`; this file explains the setup and lists the commands.

## Division of labor

| Who | Does | Doesn't |
|---|---|---|
| **Owner (Taylor)** | Sets goals, signs off every decision, labels/spot-checks discovery results, runs live checks, reads every task report | Write code or merge by hand |
| **Claude (claude.ai)** | Reads the brief and data, researches current facts, proposes decisions one at a time with trade-offs and worked examples, writes `docs/` planning files, reviews Cursor's work | Build features |
| **Cursor agent mode** | Builds one task at a time from `docs/tasks.md`, orchestrates the subagents, commits, opens MRs, merges after a green pipeline | Change the brief, decisions, PRD, architecture, or eval labels without owner approval |

Thinking happens before building: every choice is in `docs/decisions.md` (options, worked example, why, known gap) before Cursor touches it.

## Subagents (`.cursor/agents/`)

| Agent | Role | Can edit |
|---|---|---|
| design-critic | Skeptical staff engineer; challenges a design before it's built. Proceed / Proceed with changes / Rethink | nothing |
| tester | Tests first, from "done" criteria; scheduling invariants on a temp DB, retrieval recall, escalation payloads, fake-LLM failure cases, property tests | tests only |
| verifier | Runs format, lint, pyright, tests, eval smoke run, build. VERIFIED / NOT VERIFIED | nothing |
| reviewer | Correctness, spec drift, the seams (LLM adapter, retrieval interface, scheduling module), domain rules (cents, injected clock), errors, secrets, eval integrity, tests. Blocking / Should fix / Nit | nothing |
| agent-safety-auditor | Atomic bookings, member/zone rules, identity before account data, no invented facts, no internal-note leakage, safety escalation, prompt injection, public-endpoint cost limits, honest metrics. Critical/High blocks done | nothing |
| diagrammer | Mermaid diagrams in `docs/diagrams/` that match the code | diagrams only |
| explainer | `docs/system-explained.md`: plain-English walkthrough of each flow with real examples and the exec angle | that file only |
| requirements-tracer | `docs/requirements.md`: every brief requirement → decision → task → test → status | that file only |

## The loop for every task

Branch or worktree → design-critic (if undecided) → tester (tests fail first) → implement → verifier → reviewer → agent-safety-auditor (agent/tool/retrieval/scheduling/escalation/endpoint changes) → re-run after fixes → diagrammer → explainer → requirements-tracer → commit + push (MR opens) → green pipeline → `glab mr merge --auto-merge` → report. Full detail and the definition of done: `.cursor/rules/workflow.mdc`.

**Kick-off phrase:** `Do task T<n> from docs/tasks.md.` If a report comes back without the subagent results: `Follow the workflow rule.` Fresh chat every task or two.

## Where agents run (read this first)

There is **one working folder**: `~/Projects/aea_callcenter` on the owner's Mac Studio. Two ways to run Cursor in it; pick one per task.

1. **Local agent mode (recommended).** Cursor's agent in the editor, working directly in the folder. No worker, nothing stashed. You can watch files change.
2. **Cloud agent on the Mac Studio worker.** Start the worker with `agent worker start --name "mac-studio" --worker-dir ~/Projects/aea_callcenter` and pick **My Machines** at cursor.com/agents. It runs **in that same folder**, so:
   - **Commit and push everything before starting it**: starting one stashes uncommitted changes (lesson from peak6_wk1).
   - Don't edit files in the folder while it runs.
   - One cloud agent at a time, unless a task is marked parallel (then it uses its own worktree under `~/Projects/aea_callcenter-wt/<slug>`).

**Not supported: Cursor-hosted cloud VMs.** They don't have the local `.env` keys (live checks and evals need them) and GitLab here is self-hosted.

**Claude never changes git state in the working folder.** Claude commits planning docs from its own separate clone and pushes to `main`; every task starts with `git checkout main && git pull --ff-only` (workflow step 2), which picks them up. Claude reads the working folder (diffs, test output) for reviews and merges 🔒 MRs through the GitLab API. So a task branch checked out in the folder is never touched by Claude.

## The working rhythm (owner ↔ Cursor ↔ Claude)

1. Owner starts a task in Cursor: `Do task T<n> from docs/tasks.md.`
2. Cursor builds it under the workflow rule, opens the MR, and gets the pipeline green.
3. Owner tells Claude only "T<n> is done". Claude reads the branch, MR, pipeline and job logs directly.
4. Claude reviews: **full review** for 🔒 tasks (DB writes, caller identity, escalation, retrieval eval, containment eval harness, public endpoint); **spot-check** for low-risk ones. For 🔒 tasks Cursor leaves the MR open and **Claude merges it** through the GitLab API (project access token `GITLAB_TOKEN` in the local `.env`, never printed), or replies with one short list of fixes for the owner to paste into Cursor. Low-risk tasks auto-merge on green (D1).
5. After merge, Claude logs the result (decisions, ai-log) and names the next task.

Why: design and review stay independent of the agent writing the code, and nobody grades their own work.

## Enforcement

Rules are guidance; agents skip steps. So the same checks run where they can't be skipped (created in task T0):

- **Git hooks:** pre-commit format + lint on staged files; commit-msg Conventional Commits; pre-push pyright + tests.
- **GitLab CI** (`.gitlab-ci.yml`) on every push and MR: install, lint, type check, tests, secret scan. Eval smoke joins when the harness exists (T15). Never weakened to get green.
- **CI runs on the owner's Mac Studio GitLab runner with the docker executor, so Docker Desktop must be running whenever an agent pushes** (boostlingo lesson, 2026-10-17).

## Commands

From the repo root, with uv 0.13.1 and Python 3.14.8. There is no frontend (D12).

| Step | Command |
|---|---|
| Install | `uv sync` |
| Format check | `uv run ruff format --check` |
| Lint | `uv run ruff check` |
| Type check | `uv run pyright` |
| Tests | `uv run pytest` |
| Secrets scan | `docker run --rm --entrypoint "" -v "$PWD:/repo" -w /repo ghcr.io/gitleaks/gitleaks:v8.30.1 gitleaks detect --source . --redact --verbose` |
| All of the above | `scripts/ci.sh` |
| Run the app | `uv run uvicorn meridian.web.app:create_app --factory --host 127.0.0.1 --port 8000` |

Eval smoke is not listed. There is no harness until T15, so the verifier reports that step as NOT RUN.

## What the agents got wrong

Recorded in `docs/ai-log.md` (append-only), from every task report.
