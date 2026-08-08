#!/usr/bin/env python3
"""Generic {ts,ok,msg} state-file reader for a host that monitor-bridge cannot see.

Prints one tab-separated line per state file — `up<TAB>msg` or `down<TAB>msg` — for a wrapper to
push to Kuma. Always exits 0: a DOWN is data to report, not a crash, and the wrapper needs the
message either way.

Why this is generic when configarr's and janitorr's readers were bespoke: those two had to be
(configarr reads a k8s Job, janitorr reads pod logs and container uptime). This is the *third*
instance of a different and much simpler problem — a host cron writes `{ts,ok,msg}` and
monitor-bridge reads it over a `:ro` bind mount, which stops working the moment the cron changes
host. `disk_prune`, `fake_remux`, `fake_remux_replace`, `verify`, `pi_peers` and half a dozen more
in check.py are all literally this shape, so the port is a parameter, not a rewrite.

Runs under daniel-box's /usr/bin/python3 (3.12 floor — see ansible/tests/test_host_scripts_py312.py).
Carries no secrets: the push URL lives in the templated wrapper.

Usage: state_push.py <label> <state-file> <max-age-hours> [<label> <state-file> <max-age-hours> ...]
"""

from __future__ import annotations

import json
import os
import sys
import time


def verdict(state, age_s, max_age_s, label):
    """Pure: (ok, msg) for one state file. Mirrors monitor-bridge's fake_remux/disk_prune family.

    Order matters. A run that reported failure is reported as that failure even if it is also
    stale, because its own message names the cause and "N hours ago" does not.
    """
    if not state.get("ok"):
        return False, "%s: %s" % (label, state.get("msg", "?"))
    if age_s > max_age_s:
        return False, "last %s %.1fh ago (max %.1fh)" % (
            label,
            age_s / 3600.0,
            max_age_s / 3600.0,
        )
    return True, "%s ok %.1fh ago: %s" % (label, age_s / 3600.0, state.get("msg", ""))


def read_state(path):
    """(state, age_s) or (None, reason). A missing or unparseable file is a failure, not a skip —
    it is indistinguishable from a cron that has never run."""
    if not os.path.exists(path):
        return None, "no state file at %s (never ran?)" % path
    try:
        with open(path) as fh:
            state = json.load(fh)
    except (OSError, ValueError) as e:
        return None, "unreadable state file %s: %s" % (path, e)
    ts = state.get("ts")
    if not isinstance(ts, (int, float)):
        return None, "state file %s has no usable ts" % path
    return state, time.time() - ts


def main(argv) -> int:
    args = argv[1:]
    if not args or len(args) % 3:
        print("down\tstate_push.py called with %d args (need triples)" % len(args))
        return 0
    for i in range(0, len(args), 3):
        label, path, max_age_h = args[i], args[i + 1], args[i + 2]
        state, age_or_reason = read_state(path)
        if state is None:
            print("down\t%s" % age_or_reason)
            continue
        ok, msg = verdict(state, age_or_reason, float(max_age_h) * 3600, label)
        print("%s\t%s" % ("up" if ok else "down", msg))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
