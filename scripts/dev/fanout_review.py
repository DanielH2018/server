#!/usr/bin/env python3
"""Forwarding shim: `fanout_review.py <args>` runs `fanout.py review <args>` (#4346).

A `--review` unit that an older checkout's `fanout_lib.launch` started runs this path in its
batch worktree, so it forwards argv unchanged and exits with the command's own exit code. It
reads and writes nothing itself: the unit's stdin is the brief and its stdout the report. The
logic is `fanout_lib/review_unit.py`.
"""

import sys

from fanout import main

if __name__ == "__main__":
    sys.exit(main(["review", *sys.argv[1:]]))
