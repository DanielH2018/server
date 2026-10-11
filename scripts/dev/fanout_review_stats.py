#!/usr/bin/env python3
"""Forwarding shim: `fanout_review_stats.py <args>` runs `fanout.py stats <args>` (#4346).

Older checkouts and notes name this path, so it forwards argv unchanged and exits with the
command's own exit code. The logic is `fanout_lib/review_stats.py`.
"""

import sys

from fanout import main

if __name__ == "__main__":
    sys.exit(main(["stats", *sys.argv[1:]]))
