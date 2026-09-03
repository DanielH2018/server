#!/usr/bin/env python3
"""Push one heartbeat to a Kuma push monitor, as the last stage of the status-page sync.

Its own container, and the LAST one, so it runs only when every init container succeeded —
a sync that failed to read, render or apply never beats, and the tile's watchdog reports the
silence. Both healthy paths beat: a run that finds nothing to change is the common case.

Written as a file rather than an inline `python3 -c` so it can be read and linted like the
rest of the role's code. Stdlib only: this runs in the same python:3.14-alpine image the
render stage uses, with no wheels installed.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 10


def main() -> int:
    base = os.environ.get("KUMA_URL", "").rstrip("/")
    token = os.environ.get("PUSH_TOKEN", "")
    message = os.environ.get("PUSH_MESSAGE", "status page sync ok")

    if not base or not token:
        # The declaration in static-monitors.yaml.j2 is gated on the same token, so an
        # unconfigured checkout has no tile to disappoint. Say so rather than failing a sync
        # that did its work.
        print("no push token configured; skipping the heartbeat")
        return 0

    query = urllib.parse.urlencode({"status": "up", "msg": message})
    url = f"{base}/api/push/{token}?{query}"

    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            print(f"heartbeat pushed ({response.status})")
    except (urllib.error.URLError, OSError) as exc:
        # A missed beat is already an alert — the tile's watchdog reports it. Failing the Job
        # on top would turn one Kuma blip into a second, louder signal about the same thing.
        print(f"heartbeat failed: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
