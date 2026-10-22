# Threat model (draft for T16; completed in T20)

_Draft 2026-10-19. Scope: the public chat page, the agent, its tools and the staff pages. Per D20.7: every attack mapped to the OWASP Top 10 for LLM applications (2025) and classic OWASP, ranked by blast radius, with a reason for anything the red-team set does not test._

## Primary defense

The model is assumed to be fully talked around at some point, so what it *can* do is limited in code. This holds even when the prompt instructions fail:
- There are no tools for refunds, waivers or discounts.
- Tools read the customer only from the verified session (D9).
- A booking needs the caller's own words to agree to that exact option (D10.6).
- Fee consent comes only from the button or a literal "I agree" (D10.8).
- Every model-written reply passes answer check A/B (D11, D19) and the canary checks (D20.3, D20.8).

## Threats, worst first

| # | Threat | Blast radius | Main controls | Red-team cases |
|---|---|---|---|---|
| 1 | One caller sees another customer's data | Privacy breach, the worst outcome | Customer lock (D9), identity rules and the 5-failure cap (T7, T13), canary customers (D20.8), no read-back of what's on file | RT07–RT13, RT30 |
| 2 | One caller changes another customer's visit or membership | Real-world harm, money | Customer lock, writes guarded in code, two-step cancel, agreement check | RT01, RT10, RT25, RT27 |
| 3 | Money given away (waiver, discount, faked fee consent) | Revenue | No money tools; fee consent only from button or literal text | RT02, RT03, RT24 |
| 4 | Prompt injection: direct, role-play, fake staff, fake tool calls, disguised | Leads to 1–3 | Code limits above; one change per message; per-worker tool sets | RT01, RT13, RT14, RT23, RT26 and every disguised run |
| 5 | A safety or crisis call is missed or slowed down | Physical harm | Code gate before any model (D11), supervisor `safety`/`crisis` backstops for disguised text | RT21, RT22 (×4 disguises each) |
| 6 | Invented or outdated facts (prices, policy) | Wrong promises to customers | Answer check A (numbers must be in sources), B (citations), D5 precedence | RT16, RT17, RT19, RT20 |
| 7 | Instruction or tool leaks | Makes attacks easier | No tool or workflow disclosure (D20.10), instruction canary blocked in code | RT04, RT05, RT06, RT29 |
| 8 | Misrepresenting itself as a person | Trust, disclosure law | Greeting disclosure, an "I'm Meridian's virtual assistant" answer | RT28 |
| 9 | Cost and abuse (floods, long chats, spend) | Money, availability | T13 limits: per-IP rates, 30 turns, $15/day cap | RT18; limits covered by `tests/test_limits.py`, not red-team |
| 10 | Web attacks on the page or the staff dashboard | Session theft, XSS | Escaped output, strict CSP, cookie flags, Origin checks, no URL fetch (T12–T14) | RT14 (stored text), RT15 (SSRF) |

## OWASP LLM Top 10 (2025) coverage

| Risk | Covered by | Not red-teamed, and why |
|---|---|---|
| LLM01 Prompt injection | RT01, RT13, RT14, RT22, RT23, RT26, plus leetspeak/base64/crescendo variants | — |
| LLM02 Sensitive information disclosure | RT07–RT13, RT30, canary detector on every reply | — |
| LLM03 Supply chain | — | Not a conversation attack. Locked dependencies (`uv.lock`), gitleaks in CI, approved vendors only. |
| LLM04 Data and model poisoning | — | No training or fine-tuning; the KB is fixed company documents, read-only, listed in the manifest (T4). Callers can't add to it. |
| LLM05 Improper output handling | RT14 | Escaping is unit-tested in T12/T14 (model text never inserted as HTML). |
| LLM06 Excessive agency | RT01–RT03, RT10, RT15, RT24–RT27 | — |
| LLM07 System prompt leakage | RT04–RT06, RT29 | — |
| LLM08 Vector and embedding weaknesses | — | Retrieval covers company docs only, and internal docs aren't indexed (D5). The caller's search query is embedded at search time, but nothing a caller writes is ever added to the index. |
| LLM09 Misinformation | RT16, RT17, RT19, RT20, RT28 | — |
| LLM10 Unbounded consumption | RT18 (off-topic) | Rate and spend limits are tested in code (`tests/test_limits.py`); a live flood test belongs to T17 on the URL. |

**Classic OWASP:**
- **Broken access control (A01):** threats 1 and 2.
- **Injection (A03):** all SQL uses parameters; stored caller text is treated as data.
- **Security misconfiguration (A05):** T13 headers. A `curl -I` check on the live URL is in T17.
- **Authentication failures (A07):** the identity cap; one shared staff password, a known gap (D12).
- **SSRF (A10):** RT15. The agent has no fetch tool.

## Known gaps (for the writeup)

- The attack list is ours. Novel attacks are covered only by the code limits.
- Whether Railway's edge replaces or appends `X-Forwarded-For` is unknown until T17. If it appends, per-visitor limits can be dodged, though the $15/day cap still holds.
- Crisis handling is a script, not a counselor.
- Live scheduling replies get answer check A only.
- The red-team leak detector matches strings and phone digits. It would miss a leak the agent writes in base64 or leetspeak, a street number written another way, or a confirm-only answer ("yes, that's the address on file").
- The supervisor's `safety`/`crisis` backstops could wrongly flag ordinary text, for example a dusty smell on first heat-up. That is only measurable with live runs.
- Crisis phrases the code gate deliberately leaves to the supervisor, because they are too ambiguous for a code rule, e.g. "I want to end it" (it could be about a contract).
