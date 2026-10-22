# Build, deploy and alerts

_Last updated: 2026-10-20 — new: GitLab CI, the Railway deploy, startup, and the alert schedules._

```mermaid
flowchart LR
  push(["Push to GitLab"]) --> lint["lint: ruff, pyright"]
  lint --> test["test: pytest<br/>fake model, temp DBs"]
  test --> sec["security: gitleaks"]
  sec -->|"main only, token set"| dep["deploy:railway<br/>railway up (CLI 5.63.1)"]
  dep --> img["Docker image<br/>app + dataset DB + KB + index"]
  img --> entry["entrypoint.sh<br/>chown volume dir, drop to uid 10001"]
  entry --> start["create_app<br/>init_db, fee check, routes"]
  start --> vol[("/data/meridian.sqlite<br/>first boot copies dataset")]
  sched(["GitLab schedules"]) -->|"hourly :07, ALERT=health"| h["alert_check.py health<br/>/health?deep=1"]
  sched -->|"daily 8:23 CT, ALERT=conversations"| c["alert_check.py conversations<br/>$89 question, Wren slot check"]
  h --> live(["Live app"])
  c --> live
  h -->|"fail"| mail["Pipeline fails, GitLab emails owner"]
  c -->|"fail"| mail
```

**Notes.**
- CI runs on the owner's ARM Mac runner (Docker executor). Scheduled pipelines run only the alert jobs; the deploy job never runs on a schedule.
- `scripts/live_checks.sh` is run by hand after a deploy: headers, `/health`, docs routes off, and a fake `X-Forwarded-For` ignored. It makes no model calls.
