"""The SOPS write helper, which imports no other secrets_mgmt module.

`sops_set` writes one secret into a SOPS file through `sops set`, with the runner injectable.
`cluster_vars.py write` is its caller. It lives in a leaf that imports no other secrets_mgmt
module, so `rotation_tools.py` can take `REPO` and `SECRETS_FILE` from here without importing
the cluster renderer.
"""

import os
import subprocess
from collections.abc import Callable

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from lib.repo_paths import REPO as _REPO_PATH

# `str`, because every consumer builds paths with os.path.join. lib.repo_paths.REPO is a Path;
# this module is the last of the twenty call sites that module's docstring describes, which
# re-derived `parents[2]` by hand under three different names.
REPO = str(_REPO_PATH)
SECRETS_FILE = os.path.join(REPO, "secrets", "secrets.yml")


def sops_set(
    name: str,
    value: str,
    *,
    path: str = SECRETS_FILE,
    run: Callable = subprocess.run,
) -> None:
    """Write `value` as secret `name`'s plaintext in the SOPS file `path`, on stdin.

    --value-stdin keeps the new token out of argv (world-readable via /proc/<pid>/cmdline
    here — no hidepid). It still requires a JSON-encoded value, same as the old argv form, so
    the quoting stays; only the transport moves to stdin. `path` defaults to `secrets.yml`;
    `cluster_vars.py write` passes the cluster file.
    """
    run(
        ["sops", "set", "--value-stdin", path, '["%s"]' % name],
        input='"%s"' % value,
        text=True,
        check=True,
        cwd=REPO,
        # A hung `sops set` would otherwise hang `cluster_vars.py write` forever, mid-batch.
        timeout=30,
    )
