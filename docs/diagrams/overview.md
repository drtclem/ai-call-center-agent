# System overview

_Last updated: 2026-10-20 — rewritten for the built system: chat, agent graph, model adapter, hybrid search, scheduling, staff pages, monitoring, alerts and evals._

One FastAPI process on Railway serves the chat page and the staff pages. The agent runs inside it. SQLite on the Railway volume holds Meridian's tables and ours. Model providers are reached only through `agent/llm.py`.

```mermaid
flowchart LR
  caller(["Caller's browser<br/>GET / , POST /chat"])
  staffer(["Staff browser<br/>/staff (Basic auth)"])
  gitlab(["GitLab schedules<br/>alert_check.py"])

  subgraph app["FastAPI app (web/app.py)"]
    limits["limits.check_message<br/>spend cap, rates"]
    agent["agent/graph.py Agent<br/>gate, supervisor, workers, handoff"]
    tools["agent/tools.py Executor"]
    verify["verify_answer.check<br/>checks A + B, canaries"]
    staff["web/staff.py<br/>dashboard, queues, flags"]
    health["GET /health?deep=1"]
  end

  llm["agent/llm.py<br/>Sonnet / Haiku, gpt-6-astra backup"]
  search["kb/search.py<br/>BM25 + embeddings, top 4"]
  index[("kb/index<br/>37 sections, committed")]
  ident["identity.verify"]
  sched["scheduling<br/>find, book, move, cancel"]
  db[("SQLite on /data volume<br/>Meridian + app tables")]
  anthropic(["Anthropic API"])
  openai(["OpenAI API<br/>embeddings + backup"])

  caller --> limits --> agent
  agent --> llm
  agent --> tools
  agent --> verify
  tools --> search --> index
  search -->|"embed the query"| llm
  tools --> ident --> db
  tools --> sched --> db
  agent -->|"record_turn, tickets, flags"| db
  llm --> anthropic
  llm --> openai
  staffer --> staff --> db
  gitlab --> health
  gitlab -->|"daily scripted chat"| limits
  health --> db
  health -->|"list models, cached 5 min"| anthropic
```

## Legend

- **Seams.** The model adapter (`agent/llm.py`) is the only module that sends chats or embeddings; tests swap in a fake. Search is a function the agent receives (`Search`), so tests use deterministic fake vectors. All business dates come from `clock.today()`. The one exception is the `/health?deep=1` provider check, which calls the Anthropic SDK from `web/app.py`.
- **Writes.** Only `scheduling`, `db`, `monitoring`, `feedback` and `limits` write to the DB. Every account tool takes the customer from the verified session.
- **Offline, not drawn:** `kb/convert.py` and `kb/build_index.py` build `kb/converted/` and `kb/index/`. `discovery/` labels the 420 transcripts. `evals/` runs the agent in-process against temp DB copies.
- Message order: [`message-flow.md`](message-flow.md). Build and deploy: [`deploy.md`](deploy.md).
