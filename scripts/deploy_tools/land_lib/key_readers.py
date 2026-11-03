"""The setup-role files reading a key a range moved, each with the host the key reaches (#6968).

`remaining_setup_hosts` derives the roles it reports from the range's changed paths. A range
that moves only a `secrets.yml` or inventory key changes no path under a setup role, so the
tick applied the reader roles `deploy_defer.route_key_readers` routed and recorded no other
host for them. #6952 moved `host_python_version` in `group_vars/all.yml`; `initial_setup` reads
it on every host, and nothing said daniel-pi was owed the apply.

`key_readers` answers with the same derivation `secret_setup_readers.py` runs for the tick,
plus the host each key reaches. A `host_vars/<h>.yml` key reaches `<h>` alone; a `secrets.yml`
key or any other inventory key reaches every host the reader's gate admits.

The secrets half may refuse (`secret_keys.setup_reader_paths` raises `CannotNarrow`). The tick
then routed none of its readers, so this drops that half and keeps the inventory half.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import subprocess
from typing import NamedTuple

from deploy_tools.narrow_lib import broad, inventory_keys, secret_keys
from lib.narrow_git import CannotNarrow

EVERY_HOST = ""


class KeyReader(NamedTuple):
    """One setup-role file naming a moved key.

    Attributes:
        path: the repo-relative file, under `ansible/roles/setup/`.
        unread: the shippers of a vars file that read none of its moved keys, which the
            tick leaves out of `setup_roles_for(path)`.
        host: the one host the key reaches, or `EVERY_HOST` for every host the gate admits.
    """

    path: str
    unread: frozenset[str]
    host: str = EVERY_HOST


def key_readers(pr_range: str, repo: _Path) -> list[KeyReader]:
    """Every setup-role file at the range's end reading a key `pr_range` moved.

    Args:
        pr_range: `<old>..<new>`.
        repo: the checkout whose git store holds the range.

    Raises:
        CannotNarrow: the inventory half could not read the range, as for the tick.
        subprocess.CalledProcessError: `git` failed.
    """
    old, _, new = pr_range.partition("..")

    def explain(message: str) -> None:
        print(message, file=_sys.stderr)

    ctx = broad.Context(repo, new, set(), {}, explain)
    rules = secret_keys.Rules(
        broad._grep, broad._sort_hits, broad.template_importers, broad._role_tags
    )
    found = dict(inventory_keys.setup_reader_paths_by_host(old, ctx))
    try:
        secrets = secret_keys.setup_reader_paths(old, ctx, rules)
    except (CannotNarrow, subprocess.CalledProcessError) as exc:
        explain(f"key-readers: the secrets half cannot say ({exc}), so it adds no host")
    else:
        every = found.get(EVERY_HOST)
        found[EVERY_HOST] = secret_keys.merged(every, secrets) if every else secrets
    return [
        KeyReader(path, frozenset(readers.unread.get(path, ())), host)
        for host, readers in sorted(found.items())
        for path in sorted(readers.paths)
    ]


def readable_key_readers(pr_range: str, repo: _Path) -> list[KeyReader]:
    """`key_readers`, or none with a stderr line when it cannot read the range.

    The callers' other half, the reach of the range's changed paths, still answers. The tick
    parks on the same failure in `route_key_readers` before it records anything from it.
    """
    if ".." not in pr_range:
        return []
    try:
        return key_readers(pr_range, repo)
    except (CannotNarrow, subprocess.CalledProcessError) as exc:
        print(f"key-readers: none added ({exc})", file=_sys.stderr)
        return []
