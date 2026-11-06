"""mock-build-service: a tiny stand-in for a real build/test service.

It behaves like the kind of internal service a PR gate has to drive: you log
in (rate limited), ask for an ephemeral environment (capacity limited),
submit a job made of stages, poll it, read its log and clean up. Stage
outcomes come from the request, so a test matrix can describe passing,
failing and flaky (infra) scenarios. It also prints a fake credential in its
logs, the way real build tools sometimes do, so the gate's redaction is
exercised.

Standard library only. Configuration via environment:
  SERVICE_PASSWORD     password for POST /api/login (required)
  LOGIN_RATE_PER_MIN   logins allowed per client IP per minute (default 5)
  MAX_ENVS             concurrent environments allowed (default 4)
  TIME_SCALE           multiplier for stage durations (default 1.0)
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Service:
    def __init__(self, password: str, login_rate: int = 5, max_envs: int = 4, time_scale: float = 1.0) -> None:
        self.password = password
        self.login_rate = login_rate
        self.max_envs = max_envs
        self.time_scale = time_scale
        self.lock = threading.Lock()
        self.tokens: set[str] = set()
        self.logins: dict[str, deque[float]] = defaultdict(deque)
        self.envs: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.flaky_seen: set[str] = set()

    # -- auth -------------------------------------------------------------
    def login(self, client: str, password: str) -> tuple[int, dict]:
        now = time.time()
        with self.lock:
            window = self.logins[client]
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= self.login_rate:
                retry = int(60 - (now - window[0])) + 1
                return 429, {"error": "too many login attempts", "retry_after": retry}
            window.append(now)
            if not secrets.compare_digest(password, self.password):
                return 401, {"error": "invalid credentials"}
            token = secrets.token_hex(16)
            self.tokens.add(token)
        return 200, {"token": token}

    def authorized(self, header: str | None) -> bool:
        return bool(header) and header.removeprefix("Bearer ") in self.tokens

    # -- environments -----------------------------------------------------
    def create_env(self, name: str) -> tuple[int, dict]:
        with self.lock:
            if len(self.envs) >= self.max_envs:
                return 429, {"error": "capacity reached", "retry_after": 2}
            env = {"id": uuid.uuid4().hex[:12], "name": name, "created_at": time.time()}
            self.envs[env["id"]] = env
        time.sleep(0.2 * self.time_scale)  # provisioning
        return 201, env

    def delete_env(self, env_id: str) -> tuple[int, dict]:
        with self.lock:
            if self.envs.pop(env_id, None) is None:
                return 404, {"error": "no such environment"}
        return 200, {"deleted": env_id}

    # -- jobs -------------------------------------------------------------
    def submit(self, body: dict) -> tuple[int, dict]:
        env_id, stages = body.get("env_id"), body.get("stages") or []
        with self.lock:
            if env_id not in self.envs:
                return 409, {"error": "environment not found; re-select the environment"}
            job = {
                "id": uuid.uuid4().hex[:12],
                "env_id": env_id,
                "test_id": body.get("test_id", ""),
                "status": "queued",
                "exit_code": None,
                "current_stage": None,
                "stages": [{"name": s["name"], "status": "pending"} for s in stages],
                "log": [],
                "abort": threading.Event(),
            }
            self.jobs[job["id"]] = job
        threading.Thread(target=self._run, args=(job, stages), daemon=True).start()
        return 201, self.public(job)

    def _log(self, job: dict, line: str) -> None:
        with self.lock:
            job["log"].append(f"[{time.strftime('%H:%M:%S')}] {line}")

    def _sleep(self, job: dict, seconds: float) -> bool:
        """Sleep unless aborted. Returns False if the job was aborted."""
        return not job["abort"].wait(seconds * self.time_scale)

    def _run(self, job: dict, stages: list[dict]) -> None:
        if not self._sleep(job, 0.5):
            return self._finish(job, "aborted", None)
        job["status"] = "running"
        for i, stage in enumerate(stages):
            job["current_stage"] = stage["name"]
            job["stages"][i]["status"] = "running"
            self._log(job, f"==> stage {i}: {stage['name']}")
            if i == 0:
                # Real tools leak credentials like this; the gate must redact them.
                self._log(job, "+ curl --user ci-bot:tok_9f8e7d6c5b4a https://artifacts.example.com/deps.tgz")
            outcome = stage.get("outcome", "pass")
            if not self._sleep(job, float(stage.get("seconds", 1))):
                job["stages"][i]["status"] = "aborted"
                return self._finish(job, "aborted", None)
            key = f"{job['test_id']}:{stage['name']}"
            if outcome == "flaky" and key not in self.flaky_seen:
                self.flaky_seen.add(key)
                self._log(job, "fetching dependencies from mirror.example.com")
                self._log(job, "error: RPC failed; curl 56 Recv failure: Connection reset by peer")
                job["stages"][i]["status"] = "failed"
                return self._finish(job, "failed", 128)
            if outcome == "fail":
                self._log(job, "compiling src/engine/core.c")
                self._log(job, "src/engine/core.c:42:7: error: 'struct frame' has no member named 'len'")
                self._log(job, "make[2]: *** [Makefile:118: core.o] Error 1")
                self._log(job, "make: *** [all] Error 2")
                job["stages"][i]["status"] = "failed"
                return self._finish(job, "failed", 2)
            job["stages"][i]["status"] = "passed"
            self._log(job, f"stage {stage['name']} passed")
        self._finish(job, "completed", 0)

    def _finish(self, job: dict, status: str, code: int | None) -> None:
        job["status"], job["exit_code"] = status, code
        self._log(job, f"job {status} (exit {code})")

    def abort(self, job_id: str) -> tuple[int, dict]:
        job = self.jobs.get(job_id)
        if job is None:
            return 404, {"error": "no such job"}
        job["abort"].set()
        return 200, {"aborting": job_id}

    @staticmethod
    def public(job: dict) -> dict:
        return {k: v for k, v in job.items() if k not in ("log", "abort")}


def make_handler(svc: Service) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet; the gate logs what matters
            pass

        def _reply(self, status: int, body, ctype: str = "application/json") -> None:
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if status == 429 and isinstance(body, dict) and "retry_after" in body:
                self.send_header("Retry-After", str(body["retry_after"]))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return {}

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?")[0]
            if method == "GET" and path == "/healthz":
                return self._reply(200, {"status": "ok"})
            if method == "POST" and path == "/api/login":
                return self._reply(*svc.login(self.client_address[0], self._body().get("password", "")))
            if not svc.authorized(self.headers.get("Authorization")):
                return self._reply(401, {"error": "unauthorized"})
            if method == "GET" and path == "/api/envs":
                return self._reply(200, list(svc.envs.values()))
            if method == "POST" and path == "/api/envs":
                return self._reply(*svc.create_env(self._body().get("name", "env")))
            if (m := re.fullmatch(r"/api/envs/(\w+)", path)) and method == "DELETE":
                return self._reply(*svc.delete_env(m[1]))
            if method == "POST" and path == "/api/jobs":
                return self._reply(*svc.submit(self._body()))
            if m := re.fullmatch(r"/api/jobs/(\w+)(/log|/abort)?", path):
                job = svc.jobs.get(m[1])
                if job is None:
                    return self._reply(404, {"error": "no such job"})
                if method == "GET" and m[2] is None:
                    return self._reply(200, svc.public(job))
                if method == "GET" and m[2] == "/log":
                    return self._reply(200, "\n".join(job["log"]) + "\n", "text/plain")
                if method == "POST" and m[2] == "/abort":
                    return self._reply(*svc.abort(m[1]))
            self._reply(404, {"error": "not found"})

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def do_DELETE(self):  # noqa: N802
            self._dispatch("DELETE")

    return Handler


def main() -> None:
    svc = Service(
        password=os.environ["SERVICE_PASSWORD"],
        login_rate=int(os.environ.get("LOGIN_RATE_PER_MIN", "5")),
        max_envs=int(os.environ.get("MAX_ENVS", "4")),
        time_scale=float(os.environ.get("TIME_SCALE", "1.0")),
    )
    port = int(os.environ.get("PORT", "8080"))
    print(json.dumps({"event": "start", "port": port, "max_envs": svc.max_envs}), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(svc)).serve_forever()


if __name__ == "__main__":
    main()
