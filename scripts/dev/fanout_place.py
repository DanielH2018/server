#!/usr/bin/env python3
"""Forwarding shim: `fanout_place.py <args>` runs `fanout.py place <args>` (#4346).

Running fan-out batches, older checkouts and `fanout_lib.clean`'s remote leg run this path,
so it forwards its arguments unchanged and exits with the command's own exit code. The logic is
`fanout_lib/place.py`.
"""

import sys
from pathlib import Path

from fanout import main

if __name__ == "__main__":
    # `lib.cli_help` names the script from argv[0], and the place parser's own `prog` is
    # already `fanout.py place`, so the shim's refusals read as the command's own.
    sys.argv[0] = str(Path(__file__).with_name("fanout.py"))
    sys.exit(main(["place", *sys.argv[1:]]))
