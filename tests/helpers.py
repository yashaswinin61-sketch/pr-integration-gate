import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "service"), str(ROOT / "scripts")]

import app  # noqa: E402
import gate  # noqa: E402, F401  (re-exported for tests)

PASSWORD = "test-password"


class LiveService:
    """The real mock service on a random port, with stage times scaled way down."""

    def __init__(self, **kw):
        kw.setdefault("time_scale", 0.01)
        self.svc = app.Service(password=PASSWORD, **kw)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.svc))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def make_test(test_id="t1", stages=None, **kw):
    t = {
        "id": test_id,
        "timeout_seconds": 60,
        "infra_retries": 1,
        "poll_seconds": 0.01,
        "capacity_wait_seconds": 30,
        "stages": [{"name": "build", "seconds": 1}] if stages is None else stages,
    }
    t.update(kw)
    return t
