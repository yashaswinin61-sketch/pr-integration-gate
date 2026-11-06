#!/usr/bin/env python3
"""PR integration gate: drives integration tests against a build service.

Subcommands
  validate MATRIX              check the test matrix (fails CI in seconds, not hours)
  matrix   MATRIX              print the GitHub Actions strategy.matrix JSON
  run      MATRIX TEST_ID      run one test end to end, write a result file
  summary  RESULTS_DIR         Markdown results table (step summary / PR comment)

`run` lifecycle for one test:
  login (429 -> wait Retry-After) -> create environment (429 capacity -> wait)
  -> submit job -> poll, logging stage changes -> judge result
  -> on failure: fetch log, extract error lines, pick the first real cause,
     classify infra vs build; infra failures retry once on a fresh environment
  -> ALWAYS: abort the job if still running, delete the environment.
SIGTERM (a cancelled Actions run) raises SystemExit, so cleanup still happens.
Everything read from the service is redacted before it is printed or saved.

Exit codes: 0 pass, 1 build failure, 2 timeout, 3 infra (retries exhausted), 4 config.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
OUTCOMES = {"pass", "fail", "flaky"}

# Lines worth showing from a failed log, and lines that only echo an earlier error.
ERROR_RE = re.compile(r"(error[:\s]|fatal|FAILED|Traceback|No such file|\*\*\* )", re.I)
ECHO_RE = re.compile(r"make(\[\d+\])?: \*\*\*")
INFRA_PATTERNS = [
    r"Connection (reset|refused|timed out)",
    r"Could not resolve host",
    r"Temporary failure in name resolution",
    r"No space left on device",
    r"\b50[234]\b.*(Gateway|Unavailable)",
    r"TLS handshake timeout",
]
INFRA_RE = re.compile("|".join(INFRA_PATTERNS), re.I)

# The mask must not look like an error marker (make prints "*** "), or redacted
# lines would be picked up as failure causes.
REDACTIONS = [
    (re.compile(r"(--user|-u)\s+([^\s:]+):\S+"), r"\1 \2:[REDACTED]"),
    (re.compile(r"(https?://[^\s:/@]+):[^\s@]+@"), r"\1:[REDACTED]@"),
    (re.compile(r"(Authorization:\s*\w+\s+)\S+", re.I), r"\1[REDACTED]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,})"), "[REDACTED]"),
    (re.compile(r"\b(\w*(TOKEN|PASSWORD|PASS|SECRET)\w*)=\S+", re.I), r"\1=[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def gha(cmd: str, msg: str = "", **props: str) -> None:
    """Emit a GitHub Actions workflow command (harmless plain text elsewhere)."""
    p = ",".join(f"{k}={v}" for k, v in props.items())
    print(f"::{cmd}{' ' + p if p else ''}::{msg}", flush=True)


# --------------------------------------------------------------------------- matrix
def load_matrix(path: str) -> dict:
    data = json.loads(Path(path).read_text())
    defaults = data.get("defaults", {})
    tests = [{**defaults, **t} for t in data.get("tests", [])]
    return {"defaults": defaults, "tests": tests}


def validate(matrix: dict) -> list[str]:
    errors, seen = [], set()
    if not matrix["tests"]:
        errors.append("matrix has no tests")
    for i, t in enumerate(matrix["tests"]):
        where = f"tests[{i}] ({t.get('id', '?')})"
        tid = t.get("id")
        if not isinstance(tid, str) or not ID_RE.match(tid):
            errors.append(f"{where}: id must match {ID_RE.pattern}")
        elif tid in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(tid)
        stages = t.get("stages")
        if not isinstance(stages, list) or not stages:
            errors.append(f"{where}: needs at least one stage")
            continue
        names = [s.get("name") for s in stages]
        if len(set(names)) != len(names):
            errors.append(f"{where}: stage names must be unique")
        for s in stages:
            if s.get("outcome", "pass") not in OUTCOMES:
                errors.append(f"{where}: stage {s.get('name')}: outcome must be one of {sorted(OUTCOMES)}")
            if not isinstance(s.get("seconds", 1), (int, float)) or s.get("seconds", 1) < 0:
                errors.append(f"{where}: stage {s.get('name')}: seconds must be >= 0")
        if not isinstance(t.get("timeout_seconds"), int) or t["timeout_seconds"] <= 0:
            errors.append(f"{where}: timeout_seconds must be a positive integer")
        if t.get("infra_retries", 0) not in range(0, 4):
            errors.append(f"{where}: infra_retries must be 0-3")
        budget = sum(s.get("seconds", 1) for s in stages)
        if isinstance(t.get("timeout_seconds"), int) and budget >= t["timeout_seconds"]:
            errors.append(f"{where}: stages take {budget}s but timeout is {t['timeout_seconds']}s")
    return errors


def gha_matrix(matrix: dict) -> dict:
    return {"include": [{"id": t["id"]} for t in matrix["tests"] if t.get("enabled", True)]}


# --------------------------------------------------------------------------- client
class ApiError(Exception):
    def __init__(self, status: int, body: dict | str, retry_after: float | None = None):
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body, self.retry_after = status, body, retry_after


class Client:
    def __init__(self, base: str, password: str, sleep=time.sleep, log=print):
        self.base, self.password, self.sleep, self.log = base.rstrip("/"), password, sleep, log
        self.token: str | None = None

    def _call(self, method: str, path: str, body: dict | None = None, raw: bool = False):
        req = urllib.request.Request(
            self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None
        )
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode()
                return text if raw else json.loads(text or "null")
        except urllib.error.HTTPError as e:
            text = e.read().decode()
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = text
            ra = e.headers.get("Retry-After")
            raise ApiError(e.code, parsed, float(ra) if ra else None) from None

    def call(self, method: str, path: str, body: dict | None = None, raw: bool = False):
        """One automatic re-login if the session expired."""
        try:
            return self._call(method, path, body, raw)
        except ApiError as e:
            if e.status != 401 or path == "/api/login":
                raise
            self.login()
            return self._call(method, path, body, raw)

    def login(self, attempts: int = 4) -> None:
        for n in range(1, attempts + 1):
            try:
                self.token = None
                self.token = self._call("POST", "/api/login", {"password": self.password})["token"]
                return
            except ApiError as e:
                if e.status != 429 or n == attempts:
                    raise
                wait = e.retry_after or 61
                self.log(f"login rate-limited, retrying in {wait:.0f}s ({n}/{attempts})")
                self.sleep(wait)

    def create_env(self, name: str, max_wait: float) -> dict:
        deadline = time.monotonic() + max_wait
        while True:
            try:
                return self.call("POST", "/api/envs", {"name": name})
            except ApiError as e:
                if e.status != 429 or time.monotonic() > deadline:
                    raise
                wait = e.retry_after or 30
                self.log(f"capacity reached, waiting {wait:.0f}s for a free environment")
                self.sleep(wait)


# --------------------------------------------------------------------------- run
@dataclass
class Result:
    id: str
    exit: int = 4
    outcome: str = "error"
    attempts: int = 0
    job_id: str = ""
    stage: str = ""
    cause: str = ""
    seconds: float = 0.0
    error_lines: list[str] = field(default_factory=list)


def error_lines(log: str, context: int = 1, limit: int = 30) -> list[str]:
    lines = log.splitlines()
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if ERROR_RE.search(line):
            keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    return [lines[i] for i in sorted(keep)][:limit]


def first_cause(lines: list[str]) -> str:
    """The real error, not make's '*** Error 2' echo of it."""
    for line in lines:
        if ERROR_RE.search(line) and not ECHO_RE.search(line):
            return re.sub(r"^\[\d\d:\d\d:\d\d\]\s*", "", line).strip()
    return lines[-1].strip() if lines else ""


def run_once(client: Client, test: dict, attempt: int, poll: float, sleep, log) -> tuple[str, Result]:
    """One attempt on a fresh environment. Returns (kind, result); kind is pass|build|infra|timeout."""
    res = Result(id=test["id"], attempts=attempt)
    env = job = None
    started = time.monotonic()
    try:
        env = client.create_env(f"gate-{test['id']}-{attempt}", max_wait=test.get("capacity_wait_seconds", 600))
        log(f"environment {env['id']} ready")
        try:
            job = client.call(
                "POST", "/api/jobs", {"env_id": env["id"], "test_id": test["id"], "stages": test["stages"]}
            )
        except ApiError as e:
            if e.status == 409:  # environment vanished / mismatched: infra, use a new one
                res.cause = f"environment rejected: {e.body}"
                return "infra", res
            raise
        res.job_id = job["id"]
        log(f"job {job['id']} submitted")

        last_stage, deadline = None, time.monotonic() + test["timeout_seconds"]
        while True:
            job = client.call("GET", f"/api/jobs/{job['id']}")
            if job["current_stage"] != last_stage and job["current_stage"]:
                last_stage = job["current_stage"]
                log(f"stage: {last_stage}")
            if job["status"] in ("completed", "failed", "aborted"):
                break
            if time.monotonic() > deadline:
                client.call("POST", f"/api/jobs/{job['id']}/abort")
                res.stage, res.cause = last_stage or "", f"timed out after {test['timeout_seconds']}s"
                return "timeout", res
            sleep(poll)

        res.stage = job["current_stage"] or ""
        if job["status"] == "completed" and job["exit_code"] == 0:
            return "pass", res

        logtext = redact(client.call("GET", f"/api/jobs/{job['id']}/log", raw=True))
        res.error_lines = error_lines(logtext)
        res.cause = first_cause(res.error_lines) or f"job {job['status']}"
        if job["exit_code"] is None or INFRA_RE.search(logtext):
            return "infra", res
        return "build", res
    finally:
        res.seconds = round(time.monotonic() - started, 1)
        if job and job.get("status") in ("queued", "running"):
            try:
                client.call("POST", f"/api/jobs/{job['id']}/abort")
            except Exception as e:  # cleanup must never mask the real outcome
                log(f"warning: abort failed: {e}")
        if env:
            try:
                client.call("DELETE", f"/api/envs/{env['id']}")
                log(f"environment {env['id']} deleted")
            except Exception as e:
                log(f"warning: environment cleanup failed (the reaper will catch it): {e}")


def run_test(client: Client, test: dict, sleep=time.sleep, log=print) -> Result:
    client.login()
    retries = test.get("infra_retries", 1)
    for attempt in range(1, retries + 2):
        kind, res = run_once(client, test, attempt, test.get("poll_seconds", 5), sleep, log)
        if kind == "infra" and attempt <= retries:
            log(f"infrastructure failure ({res.cause}); retrying on a fresh environment")
            continue
        res.exit, res.outcome = {
            "pass": (0, "pass"),
            "build": (1, "fail"),
            "timeout": (2, "timeout"),
            "infra": (3, "infra"),
        }[kind]
        return res
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- summary
ICON = {"pass": "✅", "fail": "❌", "timeout": "⏱️", "infra": "⚠️", "error": "⚠️"}


def summary(results_dir: str, expected: list[str] | None = None) -> str:
    results = {}
    for f in sorted(Path(results_dir).glob("**/*.result.json")):
        r = json.loads(f.read_text())
        results[r["id"]] = r
    ids = expected or sorted(results)
    rows = ["| test | result | attempts | time | detail |", "|---|---|---|---|---|"]
    for tid in ids:
        r = results.get(tid)
        if r is None:
            rows.append(f"| `{tid}` | ⚠️ missing | | | no result (job cancelled or crashed) |")
            continue
        detail = (f"{r['stage']}: {r['cause']}" if r["outcome"] != "pass" else "").replace("|", "\\|")
        rows.append(
            f"| `{tid}` | {ICON.get(r['outcome'], '?')} {r['outcome']} | {r['attempts']} "
            f"| {r['seconds']:.0f}s | {detail[:200]} |"
        )
    passed = sum(1 for t in ids if results.get(t, {}).get("outcome") == "pass")
    head = f"### Integration gate: {passed}/{len(ids)} passed\n\n"
    return head + "\n".join(rows) + "\n"


# --------------------------------------------------------------------------- cli
def _sigterm(_signum, _frame):
    raise SystemExit(143)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gate")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate").add_argument("matrix")
    sub.add_parser("matrix").add_argument("matrix")
    r = sub.add_parser("run")
    r.add_argument("matrix")
    r.add_argument("test_id")
    r.add_argument("--api", default=os.environ.get("GATE_API", "http://127.0.0.1:8080"))
    r.add_argument("--out", default="results")
    s = sub.add_parser("summary")
    s.add_argument("results_dir")
    s.add_argument("--matrix", help="list tests with no result as missing")
    args = p.parse_args(argv)

    if args.cmd == "summary":
        expected = [t["id"] for t in load_matrix(args.matrix)["tests"]] if args.matrix else None
        print(summary(args.results_dir, expected), end="")
        return 0

    matrix = load_matrix(args.matrix)
    if args.cmd == "validate":
        errors = validate(matrix)
        for e in errors:
            gha("error", e, title="test matrix")
        print(f"{len(matrix['tests'])} tests, {len(errors)} problems")
        return 4 if errors else 0
    if args.cmd == "matrix":
        print(json.dumps(gha_matrix(matrix)))
        return 0

    # run
    signal.signal(signal.SIGTERM, _sigterm)
    test = next((t for t in matrix["tests"] if t["id"] == args.test_id), None)
    if test is None:
        gha("error", f"unknown test {args.test_id}")
        return 4
    password = os.environ.get("GATE_PASSWORD", "")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    res = Result(id=test["id"], cause="gate crashed before finishing")
    try:
        res = run_test(Client(args.api, password), test, log=lambda m: print(redact(m), flush=True))
    finally:
        (out / f"{test['id']}.result.json").write_text(json.dumps(asdict(res), indent=2))
    if res.error_lines:
        gha("group", f"error lines from {res.stage}")
        print("\n".join(res.error_lines))
        gha("endgroup")
    if res.outcome == "pass":
        print(f"PASS {test['id']} in {res.seconds:.0f}s ({res.attempts} attempt(s))")
    else:
        # GitHub truncates annotation titles; keep the cause at the front of the message.
        gha("error", f"{res.cause}", title=f"{test['id']} {res.outcome} at {res.stage}")
    return res.exit


if __name__ == "__main__":
    sys.exit(main())
