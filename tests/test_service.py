import json
import unittest
import urllib.error
import urllib.request

from helpers import PASSWORD, LiveService


def call(url, method="GET", body=None, token=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), e.headers


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.live = LiveService(login_rate=2, max_envs=1)

    def tearDown(self):
        self.live.close()

    def test_health_needs_no_auth_but_api_does(self):
        self.assertEqual(call(self.live.url + "/healthz")[0], 200)
        self.assertEqual(call(self.live.url + "/api/envs")[0], 401)

    def test_login_rate_limit_sends_retry_after(self):
        for _ in range(2):
            call(self.live.url + "/api/login", "POST", {"password": "wrong"})
        status, body, headers = call(self.live.url + "/api/login", "POST", {"password": PASSWORD})
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 0)

    def test_capacity_limit(self):
        _, body, _ = call(self.live.url + "/api/login", "POST", {"password": PASSWORD})
        tok = body["token"]
        self.assertEqual(call(self.live.url + "/api/envs", "POST", {"name": "a"}, tok)[0], 201)
        self.assertEqual(call(self.live.url + "/api/envs", "POST", {"name": "b"}, tok)[0], 429)

    def test_job_on_unknown_environment_is_409(self):
        _, body, _ = call(self.live.url + "/api/login", "POST", {"password": PASSWORD})
        status, _, _ = call(self.live.url + "/api/jobs", "POST", {"env_id": "nope", "stages": []}, body["token"])
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
