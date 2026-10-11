#!/usr/bin/env python3
"""Forwarding shim: `fanout_probe.py <args>` runs `fanout.py probe <args>` (#4346).

An older checkout's cc-wait `fanout` source runs this path, so it forwards argv unchanged and
exits with the command's own exit code. The logic is `fanout_lib/wait_probe.py`.
"""

import sys

from fanout import main

if __name__ == "__main__":
    sys.exit(main(["probe", *sys.argv[1:]]))
