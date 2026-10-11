#!/usr/bin/env python3
"""The interactive deploy and its two helpers: run a deploy, map tags, read a detached run.

Usage::

    deploy_cli.py [run] --tags "<service>" [...]   # what ./scripts/deploy.sh execs
    deploy_cli.py tags list|describe|validate|changed|narrow|blockers|hosts ...
    deploy_cli.py probe [--describe] <tags> [--log PATH | --log-dir DIR]

`run` is the default, so `deploy.sh --tags x` reaches it with no subcommand word and
`deploy.sh --help` prints the deploy's own usage. `deploy_cli.py tags --help` and
`deploy_cli.py probe --help` print each helper's options. Each command's logic is one module
under `deploy_lib/`, named in `_module`.

This file imports only the module the command names. `probe` runs under
`uv run --no-project` (`.claude/wait-sources/deploy`), which installs nothing, so no other
command's imports may reach it.

`deploy_tags.py` stays beside this file as a forwarding shim (#4347). The GitOps deployer's
shipped copy runs it by path (`deploy_narrow.NARROW_SCRIPT`), and so do the callers that run
it inside another checkout, which may predate this file.
"""

import sys

# Reach `deploy_tools.` from scripts/: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

COMMANDS = ("run", "tags", "probe")


def _module(command: str):
    """The module that runs `command`, imported only when that command is asked for."""
    if command == "tags":
        from deploy_tools.deploy_lib import tags

        return tags
    if command == "probe":
        from deploy_tools.deploy_lib import detach_probe

        return detach_probe
    from deploy_tools.deploy_lib import run

    return run


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    command = "run"
    if argv and argv[0] in COMMANDS:
        command, argv = argv[0], argv[1:]
    return _module(command).main(argv)


if __name__ == "__main__":
    sys.exit(main())
