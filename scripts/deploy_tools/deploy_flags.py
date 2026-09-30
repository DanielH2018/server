"""What `deploy.sh` refuses a command line over, and the exception its gates refuse with.

`deploy_run` calls `check_passthrough` once, before the tree lock. `Refused` lives here rather
than in `deploy_run.py` because this module raises it and that one only catches it, and both
are here rather than in `deploy_run.py` because that file sits at its length cap
(`ansible/tests/repo/test_module_length_ratchet.py`); this is also the only piece of the
wrapper that imports ansible itself.

It PARSES and never runs. Nothing here starts a playbook, which
`ansible/tests/deploy/test_deploy_runs_from_a_snapshot_under_service_locks.py` asserts by name
-- a module that says "ansible-playbook" outside the service locks is otherwise exactly the
shape that file exists to catch.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_flags.py
"""

import sys
from pathlib import Path

# Reach the sibling package directories: a module imported by a directly-invoked script gets
# only that script's directory, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.exit_codes import DEPLOY_BAD_FLAGS

# argparse's exit status, which is what ansible-playbook exits on a usage error. Deliberately
# NOT one of `lib.exit_codes`'s names: it is ansible's number, read on the way IN, and the
# caller maps it onto this repo's `DEPLOY_BAD_FLAGS` on the way out.
ANSIBLE_USAGE_ERROR = 2

# The playbook every deploy runs. Named here only so the parse has the positional argument it
# requires; the parse does not open it (see `usage_error`).
DEPLOY_PLAYBOOK = "ansible/deploy.yml"


class Refused(Exception):
    """A gate refused the run; nothing was deployed. `code` is the exit status."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def check_passthrough(args: list[str]) -> None:
    """Refuse with `DEPLOY_BAD_FLAGS` when ansible-playbook's parser rejects `args`."""
    if usage_error(args):
        raise Refused(DEPLOY_BAD_FLAGS)


def usage_error(args: list[str]) -> bool:
    """True when ansible-playbook's parser refuses `args`, so no playbook can run.

    Every argument the wrapper does not consume reaches ansible-playbook, which exits 2 on an
    argparse error -- the same number it uses for "a host failed". All three of the wrapper's
    run paths read that 2 as something that happened: `--check`/`--dry-run` exec
    ansible-playbook and hand its status out as the wrapper's own `DEPLOY_TAG_MISS`, while the
    locked and detached halves map it onto `DEPLOY_PLAYBOOK_FAILED` and tell the operator that
    changes applied before a failing task ARE live. Nothing ran at all -- argparse refused
    before the first play (issue #3024). Asking the parser here answers it once, ahead of the
    lock, for all three paths, and 64 is what `land.sh` exits on a bad argument too.

    `PlaybookCLI.parse()` parses and nothing else: it does not read an `-e @file`, open the
    playbook or touch inventory, so a run whose vars file exists only in the snapshot cannot be
    refused here. Measured against the ansible-core in `uv.lock` on 2026-09-30 -- `-e
    @/nonexistent.json`, a missing playbook path, `-i`, `--limit`, `--check` and `-vv` all
    parse clean, and only a usage error exits 2.

    Ansible's parser prints its own usage and the offending argument to stderr as it refuses,
    which names the fault better than this wrapper could, so nothing is printed here.
    """
    # DECIDED: this gate fails OPEN on anything but a usage error, as `clear_fact_cache` and
    # `expand_shared_roles` do. Ansible's CLI classes are not a published API, so a moved
    # import or a changed exit code leaves the deploy running exactly as it did before this
    # gate existed. Refusing every deploy over a renamed ansible module would be a worse
    # failure than the one being fixed.
    try:
        from ansible.cli.playbook import PlaybookCLI
    # SystemExit included: ansible's import asserts blocking stdio and exits where it is not.
    except Exception, SystemExit:
        return False
    try:
        PlaybookCLI(["ansible-playbook", DEPLOY_PLAYBOOK, *args]).parse()
    except SystemExit as exc:
        return exc.code == ANSIBLE_USAGE_ERROR
    # Broad on purpose, per the DECIDED note: a parse that raises anything else is not a
    # verdict about the command line.
    except Exception:
        return False
    return False
