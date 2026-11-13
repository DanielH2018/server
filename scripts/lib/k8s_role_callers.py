#!/usr/bin/env python3
"""Which k8s roles reach another k8s role, read from the live tree: `role_callers`.

A helper role has no `containers_list` entry and so no deploy tag of its own. Every reader of
this graph asks the same question of it: which tags does a change to the helper reach? The
key readers' tag mapping (`narrow_lib/role_reads.py`) reads it. `lib.k8s_roles` re-exports it, where its
readers import it from.

A template that does not read raises `CannotNarrow`, because it can hold the only edge to a
helper. The key readers refuse, the
landing dies naming the file, and the `--tags` expansion leaves the name for the tag validation
to refuse.

Split out of `lib.k8s_roles`, which sat at its length cap.

Run: uv run pytest scripts/lib/tests/test_k8s_role_callers.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
from pathlib import Path

from lib.k8s_lookups import cross_role_files
from lib.repo_paths import K8S_ROLES, REPO

__all__ = ["CannotNarrow", "role_callers"]


class CannotNarrow(Exception):
    """A role's template does not read, so the caller graph may be missing an edge.

    Import it as `lib.k8s_role_callers`, never bare: a module imported under two names is two
    classes, and an `except CannotNarrow` written against one does not catch the other.
    """


# The second edge: a key in a role's defaults makes the renderer read a helper role for it.
# `image_builder_builds` reads image-builder's context template, and
# `k8s_autodeploy_snapshot_pvcs` reads volume-snapshot's defaults for the role's snapshot Job.
_DEFAULTS_KEY_ROLES = {
    "image_builder_builds": "image-builder",
    "k8s_autodeploy_snapshot_pvcs": "volume-snapshot",
}


def _looked_up(role_dir: Path) -> set[str]:
    """Every k8s role whose file a template of `role_dir` `lookup()`s by path.

    The first edge: the owner's bytes render into the reader's manifests. sonarr's and radarr's
    `templates/verify/notification.yaml.j2` read arr-notification's script and defaults this
    way, and that lookup is the only edge arr-notification has.
    """
    found: set[str] = set()
    for src in role_dir.glob("templates/**/*.j2"):
        try:
            text = src.read_text(errors="ignore")
            found.update(path.split("/")[2] for path in cross_role_files(text))
        except OSError as exc:
            raise CannotNarrow(f"{src} does not load: {exc}") from exc
    return found


def _read_through_defaults(role_dir: Path) -> set[str]:
    """Every helper role the renderer reads for a key `role_dir`'s defaults set."""
    defaults = role_dir / "defaults" / "main.yml"
    if not defaults.is_file():
        return set()
    text = defaults.read_text(errors="ignore")
    return {
        role
        for key, role in _DEFAULTS_KEY_ROLES.items()
        if re.search(rf"^{key}:", text, re.MULTILINE)
    }


def role_callers(repo: Path | str | None = None) -> dict[str, set[str]]:
    """For each k8s role another role `lookup()`s or reads through its defaults, those roles.

    A helper role has no `containers_list` entry and so no deploy tag of its own, but a
    caller's deploy applies it: a lookup or a defaults key renders its file into the caller's
    manifests. A change to it is therefore applied by deploying its callers — which is what `scripts/deploy_tools/land_lib/land_tags.py` asks
    this to decide, so that landing a helper-role change alongside all of its callers stops
    reading as `needs-manual-apply`.

    Only ``k8s/roles`` is walked.

    ``repo`` names the checkout to walk, for a caller that reads a tree other than the one
    this module was imported from (``role_reads.context_for(ref, cwd)``). Omitted, it walks
    ``lib.repo_paths.K8S_ROLES``, which is this file's own checkout.

    Raises:
        CannotNarrow: a template does not read. It can hold the only edge from a caller to a
            helper, and a graph short by that edge names too few tags.
    """
    roles_dir = K8S_ROLES if repo is None else Path(repo) / K8S_ROLES.relative_to(REPO)
    callers: dict[str, set[str]] = {}
    if not roles_dir.is_dir():
        return callers
    # DECIDED: a lookup into a DECLARED role is an edge too (authelia and traefik ->
    # crowdsec), rather than filtered out. Filtering would need the
    # walked tree's host_vars, which the throwaway tree a test walks does not have. The
    # landing, the deployer, the broad narrowing and the `--tags` expansion stop at a declared
    # role, so they never read such an edge. `releases_consumers._closure` does not stop, and
    # it reads the edge only for a census role whose closure passes through crowdsec or
    # monitor-bridge.
    for role_dir in sorted(p for p in roles_dir.iterdir() if p.is_dir()):
        caller = role_dir.name
        reached = _looked_up(role_dir) | _read_through_defaults(role_dir)
        for callee in reached - {caller}:
            callers.setdefault(callee, set()).add(caller)
    return callers
