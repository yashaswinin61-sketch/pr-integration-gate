import json
import tempfile
import time
import unittest
from pathlib import Path

from helpers import PASSWORD, LiveService, gate, make_test


class RunTest(unittest.TestCase):
    def setUp(self):
        self.live = LiveService()
        self.logs = []
        self.client = gate.Client(
            self.live.url, PASSWORD, sleep=lambda s: time.sleep(min(s, 0.05)), log=self.logs.append
        )

    def tearDown(self):
        self.live.close()

    def run_test(self, test):
        return gate.run_test(self.client, test, sleep=lambda s: time.sleep(s), log=self.logs.append)

    def test_pass_logs_stages_and_cleans_up(self):
        res = self.run_test(make_test(stages=[{"name": "a", "seconds": 1}, {"name": "b", "seconds": 1}]))
        self.assertEqual((res.exit, res.outcome, res.attempts), (0, "pass", 1))
        self.assertIn("stage: a", self.logs)
        self.assertEqual(self.live.svc.envs, {}, "environment must be deleted")

    def test_build_failure_is_not_retried_and_cause_skips_make_echo(self):
        res = self.run_test(make_test(stages=[{"name": "compile", "seconds": 1, "outcome": "fail"}]))
        self.assertEqual((res.exit, res.outcome, res.attempts), (1, "fail", 1))
        self.assertEqual(res.stage, "compile")
        self.assertIn("has no member named 'len'", res.cause)
        self.assertNotIn("*** [all]", res.cause)

    def test_infra_failure_retries_on_fresh_environment(self):
        res = self.run_test(make_test(stages=[{"name": "fetch", "seconds": 1, "outcome": "flaky"}]))
        self.assertEqual((res.exit, res.outcome, res.attempts), (0, "pass", 2))
        self.assertTrue(any("retrying on a fresh environment" in m for m in self.logs))

    def test_infra_failure_with_no_retries_left(self):
        res = self.run_test(make_test(infra_retries=0, stages=[{"name": "fetch", "seconds": 1, "outcome": "flaky"}]))
        self.assertEqual((res.exit, res.outcome), (3, "infra"))
        self.assertIn("Connection reset", res.cause)

    def test_secrets_in_job_log_are_redacted(self):
        res = self.run_test(make_test(stages=[{"name": "x", "seconds": 1, "outcome": "fail"}]))
        joined = "\n".join(res.error_lines + self.logs)
        self.assertNotIn("tok_9f8e7d6c5b4a", joined)

    def test_timeout_aborts_job_and_deletes_environment(self):
        res = self.run_test(make_test(timeout_seconds=1, stages=[{"name": "slow", "seconds": 1000}]))
        self.assertEqual((res.exit, res.outcome), (2, "timeout"))
        self.assertEqual(self.live.svc.envs, {})
        job = next(iter(self.live.svc.jobs.values()))
        self.assertTrue(job["abort"].is_set())

    def test_login_rate_limit_is_waited_out(self):
        self.live.close()
        self.live = LiveService(login_rate=1)
        waits = []
        self.client = gate.Client(self.live.url, PASSWORD, sleep=waits.append, log=self.logs.append)
        self.client.login()
        # The second login is rate limited; sleep is stubbed, so the window never clears.
        with self.assertRaises(gate.ApiError) as ctx:
            self.client.login(attempts=2)
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(len(waits), 1)
        self.assertGreater(waits[0], 0)

    def test_capacity_wait_then_success(self):
        self.live.close()
        self.live = LiveService(max_envs=1)
        self.client = gate.Client(
            self.live.url, PASSWORD, log=self.logs.append, sleep=lambda s: self.live.svc.envs.clear()
        )
        self.client.login()
        self.client.create_env("blocker", max_wait=5)
        env = self.client.create_env("second", max_wait=5)  # waits once, blocker is "released"
        self.assertEqual(env["name"], "second")
        self.assertTrue(any("capacity reached" in m for m in self.logs))

    def test_session_expiry_relogs_in_once(self):
        self.client.login()
        self.live.svc.tokens.clear()
        self.assertEqual(self.client.call("GET", "/api/envs"), [])


class MatrixTest(unittest.TestCase):
    def test_repo_matrix_is_valid(self):
        m = gate.load_matrix(str(Path(__file__).resolve().parents[1] / ".github/integration-tests.json"))
        self.assertEqual(gate.validate(m), [])

    def test_validation_errors(self):
        m = {
            "tests": [
                make_test("Bad_ID"),
                make_test("dup"),
                make_test("dup"),
                make_test("x", stages=[{"name": "a", "outcome": "explode"}]),
                make_test("y", timeout_seconds=5, stages=[{"name": "a", "seconds": 10}]),
                make_test("z", stages=[]),
            ]
        }
        errors = "\n".join(gate.validate(m))
        for needle in ("id must match", "duplicate id", "outcome must be", "timeout is 5s", "at least one stage"):
            self.assertIn(needle, errors)

    def test_defaults_merge_and_disabled_tests_are_skipped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(
                {
                    "defaults": {"timeout_seconds": 9},
                    "tests": [
                        {"id": "a", "stages": [{"name": "s"}]},
                        {"id": "b", "enabled": False, "stages": [{"name": "s"}]},
                    ],
                },
                f,
            )
        m = gate.load_matrix(f.name)
        self.assertEqual(m["tests"][0]["timeout_seconds"], 9)
        self.assertEqual(gate.gha_matrix(m), {"include": [{"id": "a"}]})


class TextTest(unittest.TestCase):
    def test_redact(self):
        cases = {
            "curl --user bot:abc123 https://x": "curl --user bot:[REDACTED] https://x",
            "git clone https://me:hunter2@example.com/r.git": "git clone https://me:[REDACTED]@example.com/r.git",
            "Authorization: Bearer abc.def": "Authorization: Bearer [REDACTED]",
            "export API_TOKEN=xyz": "export API_TOKEN=[REDACTED]",
            "token ghp_" + "a" * 36: "token [REDACTED]",
        }
        for raw, want in cases.items():
            self.assertEqual(gate.redact(raw), want)

    def test_first_cause_prefers_real_error(self):
        lines = [
            "[10:00:00] make[2]: *** [Makefile:1: a.o] Error 1",
            "[10:00:00] a.c:1:1: error: boom",
            "make: *** [all] Error 2",
        ]
        self.assertEqual(gate.first_cause(lines), "a.c:1:1: error: boom")

    def test_summary_marks_missing_results(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.result.json").write_text(
                json.dumps({"id": "a", "outcome": "pass", "attempts": 1, "seconds": 3, "stage": "", "cause": ""})
            )
            Path(d, "b.result.json").write_text(
                json.dumps(
                    {"id": "b", "outcome": "fail", "attempts": 1, "seconds": 9, "stage": "build", "cause": "x | y"}
                )
            )
            text = gate.summary(d, ["a", "b", "c"])
        self.assertIn("1/3 passed", text)
        self.assertIn("| `c` | ⚠️ missing", text)
        self.assertIn("build: x \\| y", text)


class CliTest(unittest.TestCase):
    def test_run_writes_result_file_and_exit_code(self):
        live = LiveService()
        try:
            with tempfile.TemporaryDirectory() as d:
                matrix = Path(d, "m.json")
                matrix.write_text(
                    json.dumps(
                        {"tests": [make_test("cli", stages=[{"name": "build", "seconds": 1, "outcome": "fail"}])]}
                    )
                )
                import os

                os.environ["GATE_PASSWORD"] = PASSWORD
                rc = gate.main(["run", str(matrix), "cli", "--api", live.url, "--out", d])
                result = json.loads(Path(d, "cli.result.json").read_text())
        finally:
            live.close()
        self.assertEqual(rc, 1)
        self.assertEqual(result["outcome"], "fail")


if __name__ == "__main__":
    unittest.main()
