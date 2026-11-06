# pr-integration-gate

![pr-gate](https://github.com/yashaswinin61-sketch/pr-integration-gate/actions/workflows/pr-gate.yml/badge.svg)

A **GitHub Actions pull-request gate for long-running integration tests**. Every PR builds the
service from its own code, then runs a matrix of end-to-end tests against throwaway instances of
it. The PR can merge only when a single required check, `gate`, is green.

This is a public, self-contained version of a pattern I built for real: a PR gate where each test
is a multi-hour build on a fresh cloud VM. The real service is swapped for a small **mock build
service** (`service/`) with the same awkward behaviour (login rate limits, capacity limits,
multi-stage jobs, flaky infrastructure, credentials leaking into logs), so the whole pipeline
runs on free GitHub-hosted runners in a few minutes.

## Pipeline

```
 pull_request ─► fast-checks ─► build-service ─► integration (dynamic matrix: one job per test) ─┐
                 actionlint      image from        start isolated instance (composite action)    │
                 ruff            the PR's code,    gate.py run <test>: login → env → job → poll  │
                 validate matrix shared as an      → triage → retry infra once → always clean up │
                 unit tests      artifact          upload result + logs (14 days)                │
                 plan matrix                                                                     ▼
                                                    report: step summary + sticky PR comment ◄───┤
                                                    gate:   the ONE required check ◄─────────────┘
```

Example PR comment (updated in place on every run):

> ### Integration gate: 5/6 passed
> | test | result | attempts | time | detail |
> |---|---|---|---|---|
> | `linux-x86-64` | ✅ pass | 1 | 31s | |
> | `flaky-mirror` | ✅ pass | 2 | 33s | |
> | `windows-cross` | ❌ fail | 1 | 9s | mingw-build: src/engine/core.c:42:7: error: 'struct frame' has no member named 'len' |

## GitHub Actions techniques used

| technique | where / why |
|---|---|
| **Dynamic matrix** | `fast-checks` turns `.github/integration-tests.json` into `strategy.matrix` via a job output and `fromJSON`. Adding a test is a one-line JSON change, with no YAML edits |
| **Build once, test many** | the service image is built from the PR's code once, `docker save`d to an artifact, and loaded by every matrix job |
| **Composite action** | `.github/actions/start-service`: load image, mint per-instance credentials, `docker compose up --wait` |
| **Concurrency** | one run per PR. A new push cancels the outdated run (`cancel-in-progress`) |
| **Least-privilege `GITHUB_TOKEN`** | `contents: read` at the top level. Only `report` gets `pull-requests: write` |
| **`fail-fast: false`** | one red test doesn't hide the others |
| **`always()` vs `!cancelled()`** | teardown and log upload use `always()`. `report` and `gate` use `!cancelled()`, so a cancelled run releases the concurrency group instead of queueing jobs |
| **Single required check** | branch rules require only `gate`, which **runs and fails** rather than being skipped, because GitHub treats a *skipped* required check as passing |
| **Fork safety** | fork PRs never reach the jobs that run code. `gate` fails them explicitly |
| **Re-run failed jobs** | each test is its own matrix job, so "Re-run failed jobs" retries only the red tests and keeps the green ones |
| **Secrets hygiene** | a random password per instance, `::add-mask::`, `umask 077` env file deleted in teardown, and all service output redacted |
| **Workflow commands** | `::error title=…::` annotations carry the failure cause, `::group::` folds the error lines, `$GITHUB_STEP_SUMMARY` gets the table |
| **github-script** | creates or updates one marked PR comment rather than spamming new ones |
| **Scheduled safety net** | `stale-env-reaper.yml` (cron at `:17`, plus `workflow_dispatch` with a dry-run input) deletes environments leaked by killed jobs |
| **actionlint** | the workflows are linted in CI |

## What `gate.py run` does for one test

1. **Login**: on `429` it waits for `Retry-After`. A `401` mid-run triggers one re-login.
2. **Create an environment**: on `429 capacity reached` it waits for a free slot instead of failing.
3. **Submit and poll** the job, logging each stage change live, with a per-test timeout. On timeout it **aborts** the job so it doesn't hold capacity.
4. **Triage a failure**:
   - fetch the full log, **redact** it, and extract the error lines with context;
   - pick the **first real cause** (`core.c:42: error: …`), not make's `*** Error 2` echo of it;
   - classify it: **infrastructure** (connection reset, DNS, disk full, 5xx, lost job) gets **one retry on a fresh environment**, while a **build failure** fails immediately.
5. **Always clean up** (`finally:`): abort the job if it's still running, then delete the environment. A SIGTERM handler turns a cancelled Actions run into `SystemExit`, so cleanup still runs.
6. Write `<id>.result.json` for the report job.

Exit codes: `0` pass, `1` build failure, `2` timeout, `3` infra (retries used up), `4` config error.

## Lessons from running this for real

- **A skipped required check counts as passing.** If `gate` were skipped (by a disabled-flag
  variable, or a fork PR), the PR could merge untested. So `gate` always runs, and fails if
  anything is missing.
- **`always()` on the final job kept cancelled runs alive** and blocked the next push's run in
  the concurrency group. `!cancelled()` fixed it.
- **Ten parallel logins tripped the service's rate limit.** The fix was retrying on `429` and
  staggering starts.
- **Container DNS search domains** made a short hostname resolve to a corporate host with the
  same name. `dns_search: .` in compose fixed it.
- **Cold starts were slower than the healthcheck window**, so a generous `start_period` was needed.
- **Parallel instances on one runner collided** on host ports and compose project names. They
  now get a unique project name and a port bound to localhost per job.
- **Build tools print credentials** (`curl --user acct:token`). Logs and artifacts are readable
  by anyone with repo access, so everything goes through `redact()`. One bug worth knowing: the
  redaction mask must not look like an error marker. `***` matched make's `*** ` and got
  reported as the failure cause, so the mask is now `[REDACTED]`.
- **Commit status descriptions are cut at 140 characters**, so put the cause where it survives.

## Run it locally

```bash
python -m unittest discover -s tests -v        # 21 tests: gate, service, reaper
docker compose -f service/compose.yml up -d --build      # needs a .env with SERVICE_PASSWORD=...
GATE_PASSWORD=... python scripts/gate.py run .github/integration-tests.json flaky-mirror
```

The test matrix (`.github/integration-tests.json`) has a JSON Schema, so editors autocomplete and
flag mistakes. CI enforces the same rules plus cross-field checks (for example, stage time must
fit inside the timeout).

## License

MIT
