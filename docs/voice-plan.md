# Voice plan (phone calls)

> **Not built — design only.** Decided in D18 (2026-10-18): the submission is chat-only; this is how voice would be added. Owner approval needed before any of it is built.

## Shape

```
caller's phone → phone service (e.g. Twilio number) → speech-to-text (streaming)
   → the same agent (safety check → supervisor → worker → answer checks)
   → text-to-speech (streaming) → caller
```

The agent, tools, identity rules (D9), scheduling rules (D10), escalation (D11), answer checks (D19) and safety rules (D20) are reused unchanged. Voice is a new front door, not a new agent.

## What changes for voice

| Problem | Plan |
|---|---|
| **Speed** (silence over ~1.5–2 s feels broken) | Haiku as the brain if it matched Sonnet in the D7 comparison; stream speech out as soon as the first sentence is ready; a short filler ("let me check that") when a tool call is running. Target and measure "caller stops talking → agent starts talking". |
| **Mishearing names, numbers, times** | Read back every detail before acting ("that's 5-1-2, 5-5-5, 0-1-0-1?"); spell-back for names that match more than one customer; times confirmed as words ("Tuesday the 20th, between 12 and 4"). |
| **Interruptions** | Stop speaking when the caller starts (barge-in); keep the pending slot offer in shared state so nothing is lost. |
| **Formal fee consent (D10)** | The amount is read back and the caller says "I agree"; that recorded turn is the consent record. |
| **Safety (D11)** | The safety script is spoken immediately on the first matching phrase. |
| **Handoff** | A real warm transfer to a Meridian phone line, with the handoff ticket attached, instead of the chat ticket card. |
| **Identity** | Caller ID added as a silent extra check (the known gap in D9). |
| **AI disclosure** | "Hi, you've reached Meridian's virtual assistant" in the greeting (D20). |

## How it would be measured

- Response latency per turn (p50/p95), from end of caller speech to start of agent speech.
- Word error rate on names, phone numbers and dates from a small set of recorded test calls.
- The same conversation test set (D4), replayed through text-to-speech → speech-to-text, to measure how much containment drops on voice.

## Decisions it would need first

Phone provider; speech-to-text and text-to-speech providers (the owner has Deepgram and ElevenLabs keys from boostlingo); latency target; whether the conversation recordings are stored (privacy, D17).
