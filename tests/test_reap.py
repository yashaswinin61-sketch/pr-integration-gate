import os
import time
import unittest

import reap
from helpers import PASSWORD, LiveService


class ReapTest(unittest.TestCase):
    def test_deletes_only_old_gate_envs(self):
        live = LiveService(max_envs=10)
        try:
            svc = live.svc
            for name, age in [("gate-old", 300), ("gate-new", 1), ("someone-else", 300)]:
                env_id = name.replace("-", "")
                svc.envs[env_id] = {"id": env_id, "name": name, "created_at": time.time() - age * 60}
            os.environ["GATE_PASSWORD"] = PASSWORD
            reap.main(["--api", live.url, "--max-age-minutes", "120"])
            self.assertEqual(sorted(e["name"] for e in svc.envs.values()), ["gate-new", "someone-else"])
        finally:
            live.close()


if __name__ == "__main__":
    unittest.main()
