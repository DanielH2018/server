#!/usr/bin/env python3
"""Forwarding shim: `deploy_tags.py <args>` runs `deploy_cli.py tags <args>` (#4347).

The GitOps deployer's shipped copy runs this path (`deploy_narrow.NARROW_SCRIPT`), and so do
the callers that run it inside another checkout, which may predate `deploy_cli.py`: the
landing in the primary checkout, `deploy.sh --list-services` and the locked half's tag list
in a snapshot. It forwards argv unchanged and exits with the command's own exit code. The
logic is `deploy_lib/tags.py`.
"""

import sys

from deploy_cli import main

if __name__ == "__main__":
    sys.exit(main(["tags", *sys.argv[1:]]))
