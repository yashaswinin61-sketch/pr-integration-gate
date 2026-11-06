#!/usr/bin/env python3
"""Delete gate environments older than a TTL (leftovers from killed jobs)."""

from __future__ import annotations

import argparse
import os
import sys
import time

from gate import Client


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default=os.environ.get("GATE_API", "http://127.0.0.1:8080"))
    p.add_argument("--max-age-minutes", type=float, default=120)
    p.add_argument("--prefix", default="gate-")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    client = Client(args.api, os.environ.get("GATE_PASSWORD", ""))
    client.login()
    now, reaped = time.time(), 0
    for env in client.call("GET", "/api/envs"):
        age_min = (now - env["created_at"]) / 60
        if not env["name"].startswith(args.prefix) or age_min < args.max_age_minutes:
            continue
        print(f"{'would delete' if args.dry_run else 'deleting'} {env['name']} ({age_min:.0f} min old)")
        if not args.dry_run:
            client.call("DELETE", f"/api/envs/{env['id']}")
        reaped += 1
    print(f"{reaped} stale environment(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
