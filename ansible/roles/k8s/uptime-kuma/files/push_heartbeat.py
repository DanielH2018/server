#!/usr/bin/env python3
"""Push one heartbeat to a Kuma push monitor, as the last stage of the status-page sync.

Its own container, and the LAST one, so it runs only when every init container succeeded —
a sync that failed to read, render or apply never beats, and the tile's watchdog reports the
silence. Both healthy paths beat: a run that finds nothing to change is the common case.

Written as a file rather than an inline `python3 -c` so it can be read and linted like the
rest of the role's code. Stdlib only: this runs in the same python:3.14-alpine image the
render stage uses, with no wheels installed. The push itself is setup/common's `kuma_push.py`,
staged beside this file in the same ConfigMap, so the beat gets the host pushers' retry
through an uptime-kuma rollout's 404 window and their message cap (#3745). The retry adds at
most 90s to a run whose Kuma is down, which the job deadline's margin over two dump budgets
absorbs on the first pod; only a second pod's beat can meet the deadline, and only while Kuma
is down, when the tile is already going DOWN.

Usage:

    python3 /config/push_heartbeat.py

Takes no arguments. The status-page-sync CronJob runs it as its last container and always
exits 0. `-h` and `--help` print this text and exit 0.

Environment:

    KUMA_URL      base URL of Uptime Kuma; with an empty value the beat is skipped
    PUSH_TOKEN    token of the Kuma push monitor; with an empty value the beat is skipped
    PUSH_MESSAGE  message sent with the beat (default "status page sync ok")
"""

from __future__ import annotations

import os
import sys

from kuma_push import kuma_push


def main() -> int:
    base = os.environ.get("KUMA_URL", "")
    token = os.environ.get("PUSH_TOKEN", "")
    message = os.environ.get("PUSH_MESSAGE", "status page sync ok")

    if not base or not token:
        # The declaration in static-monitors.yaml.j2 is gated on the same token, so an
        # unconfigured checkout has no tile to disappoint. Say so rather than failing a sync
        # that did its work.
        print("no push token configured; skipping the heartbeat")
        return 0

    # A missed beat is already an alert — the tile's watchdog reports it. Failing the Job on
    # top would turn one Kuma blip into a second, louder signal about the same thing, so a lost
    # push exits 0 too.
    if kuma_push(
        "up", message, base, token, log=lambda line: print(line, file=sys.stderr)
    ):
        print("heartbeat pushed")
    return 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(main())
