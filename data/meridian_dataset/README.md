# Meridian Home Comfort — FDE Challenge Dataset

Meridian Home Comfort is a (fictional) residential HVAC and plumbing services company in the Austin, TX metro. Its call center is capacity constrained and the exec team wants an AI agent to handle the bulk of routine inbound calls.

This folder contains everything you have been given. Nothing has been cleaned for you.

## Contents

```
call_transcripts/            420 inbound call transcripts, July–Sept 2026 (plain text, one file per call)
call_transcripts_index.csv   call_id, timestamp, agent, duration — no labels
knowledge_base/              17 files the company uses internally and with customers
scheduling_db/               SQLite database + CSV exports + SCHEMA.md
```

## Call transcripts
Each file has a short header (call ID, timestamp, agent, duration) followed by `Agent:` / `Customer:` turns. Transcripts were produced by an STT system and contain filler and occasional interruptions. They are **not labeled by intent** — identifying the call drivers is part of the job.

## Knowledge base
A dump of the company's shared drive. It contains markdown, plain text, a PDF, a CSV, and an image. Some documents are current, some are not, and some contradict each other. There is no index. The `internal/` folder is agent-facing only and should not be read verbatim to customers.

## Scheduling database
`scheduling_db/SCHEMA.md` documents the tables, the status values, the zone mapping, and the expected booking behavior. Four seeded test customers are listed there for your own testing. The reviewer will test the live agent against the same database, so bookings must actually write to it.

## Dates
Treat **2026-10-16** as "today." Appointment slots run for the three weeks following.
