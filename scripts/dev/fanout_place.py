#!/usr/bin/env python3
"""Forwarding shim: `fanout_place.py <args>` runs `fanout.py place <args>` (#4346).

Running fan-out batches, older checkouts and `fanout_lib.clean`'s remote leg run this path,
so it forwards argv unchanged and exits with the command's own exit code. The logic is
`fanout_lib/place.py`.
"""

import sys

from fanout import main

if __name__ == "__main__":
    sys.exit(main(["place", *sys.argv[1:]]))
