# One caller message

_Last updated: 2026-10-20 — new: the order a message goes through, from limits to the recorded turn._

```mermaid
flowchart TD
  msg(["POST /chat"]) --> lim{"limits.check_message<br/>cap, length, rate, turns"}
  lim -->|refused| limmsg["Limit message<br/>no model call"]
  lim -->|ok| gate{"Code gate (_gate)"}
  gate -->|"safety or crisis phrase"| ho
  gate -->|"waiting for callback time"| cb["Save callback window"]
  gate -->|"chat already handed off"| closed["'A team member will follow up'"]
  gate -->|"asks for a person or manager"| ho
  gate -->|"fee consent pending"| fee{"Button or typed 'I agree'?"}
  fee -->|yes| change["Change + fee_reviews row<br/>one transaction"]
  fee -->|"no, first time"| again["Notice again"]
  fee -->|"no, second time"| ho
  gate -->|otherwise| sup{"Supervisor (Haiku)<br/>route()"}
  sup -->|handoff + reason| ho
  sup -->|kb| kb["KB worker (Sonnet)<br/>search_kb, answer"]
  sup -->|scheduling| sch["Scheduling worker (Sonnet)<br/>verify_identity, find_slots, book_slot..."]
  kb --> chk{"verify_answer<br/>A + B"}
  sch -->|"model-written reply"| chkA{"verify_answer<br/>A"}
  sch -->|"code-written: read-back,<br/>fee notice"| out
  chk -->|pass| out["Reply"]
  chkA -->|pass| out
  chk -->|"fail: retry once, then"| ho
  chkA -->|"fail: retry once, then"| ho
  kb -->|request_handoff| ho
  sch -->|"request_handoff or tool can't"| ho
  ho["Handoff (code)<br/>ticket + what/why/next"] --> out
  out --> rec[("record_turn<br/>outcome, cost, flags")]
```

**Notes.**
- The gate checks safety and crisis before anything else, even in a closed chat. A model failure (after one retry and the backup) or a crash also ends in the handoff box.
- Scheduling replies get check A only. KB answers get A and B. Code-written texts aren't checked.
- Booking happens only inside `book_slot` when `confirm.agreed_option` matches one offered option; otherwise code sends "Just to confirm: …?".
