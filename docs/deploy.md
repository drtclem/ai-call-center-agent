# Deploying to Railway (T17)

The app runs as one Railway service built from the `Dockerfile`, with a volume for the database (D13).

## One-time setup (owner)

1. Rotate the GitLab runner-305 token (see the T13/T14 notes) before connecting anything new.
2. From `~/Projects/aea_callcenter`, run `railway init`, then create a service and attach a **volume** mounted at `/data`.
3. Set these Railway variables. Values are never committed:
   - `ENV=production`
   - `DB_PATH=/data/meridian.sqlite`
   - `ANTHROPIC_API_KEY`
   - `OPENAI_API_KEY` (the fallback model)
   - `STAFF_PASSWORD` (16+ characters)
   - `REVIEWER_PASSWORD` (optional, 16+ characters, different): a read-only login for reviewers, published in the README
   - `VISITOR_SALT` (any long random string; without it each restart gets a new salt, which resets the per-visitor limits)
4. Create a Railway **project token** (deploy-only). In GitLab → Settings → CI/CD → Variables, add it as `RAILWAY_TOKEN` (masked, protected), and add `RAILWAY_SERVICE` with the service name.
5. `RAILWAY_CLI_VERSION` in `.gitlab-ci.yml` is pinned to 5.63.1 (released 2026-09-28; its linux-arm64 build was tested on 2026-10-20). Version 4.2.0 failed because it has no build for the ARM runner.

After that, every green pipeline on `main` deploys (job `deploy:railway`). Before the token exists, the job doesn't run.

## How the container starts

`scripts/entrypoint.sh` starts as root only long enough to create the `DB_PATH` folder and give it to the app user, because Railway mounts volumes owned by root. Then it drops to uid 10001 with `setpriv`. The first boot copies the dataset DB to `DB_PATH`, and later boots keep it (D13).

The image contains:
- the dataset DB
- the KB documents
- `kb/manifest.toml`
- `kb/converted/`
- `kb/index/`

`tests/test_deploy.py` checks the Dockerfile copies everything the app reads at startup.

## Checks after the first deploy (free: no chat messages)

```
STAFF_PASSWORD=... scripts/live_checks.sh https://<app>.up.railway.app
```

It checks:
- the page answers 200
- the HSTS and CSP headers are present
- there's no `server` header
- `/health` is ok
- the docs routes are off
- a fake `X-Forwarded-For` doesn't change the client IP the limits use

Then, by hand:
- **Bookings survive a redeploy (R-31).** Book a visit for a canary customer, redeploy, and confirm it's still there. This costs one short conversation, about 3¢. Afterwards, update D13 from provisional to confirmed.
- **Logs.** Trigger an error and check that the Railway logs show only the exception type and traceback lines, no message text (`NoExceptionText` in `web/app.py`).

## Alerts (T18, D15.6)

The alerts are GitLab scheduled pipelines. Under CI/CD → Schedules, create two schedules on `main`:
- **hourly**, with the variable `ALERT=health`: runs `scripts/alert_check.py health`, which hits `/health?deep=1` (the DB is read; the model provider is checked with a free "list models" call, cached for 5 minutes).
- **daily**, with the variable `ALERT=conversations`: one KB question, plus a slot check as the canary customer Wren Halvorsen (no booking is made). About 5¢ a day.

Also add the project CI variable `APP_URL` (the live URL). A failed job fails the pipeline, and GitLab emails you.

To prove the alert works (T18 done criterion), run the hourly schedule once with `APP_URL` pointed at a URL that returns 503, and check that the email arrives.

**Known limits of the alerts:**
- They run on the owner's Mac Studio runner. If the Mac is asleep or offline, the scheduled pipelines wait, and nothing alerts. An outside uptime monitor would cover that.
- The daily conversations are real traffic. They count in the dashboard, containment and spend (about 5¢ a day), and show as the canary customer Wren Halvorsen.
