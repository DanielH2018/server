"""Who holds a copy of a secret, measured from the tree.

`tree_consumers()` answers "who is still holding the old value?" for every secret with a grep
over the roles and host components — the question a rotation actually poses, and the one
`audit` does not answer. `consumer_commands()` turns that answer into the apply command per
plane.

Both are pure reads: nothing here decrypts, writes or shells out.
"""

import os
from functools import lru_cache

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from secrets_mgmt.rotation_tools import REPO
from lib import yaml_fast
from lib.k8s_lookups import k8s_lookup_map


# The two planes a consumer lives on. A k8s role is applied by Flux from the cluster file
# (`FLUX_PATH`). A host package carries the value in its host's SOPS file, which only the
# operator's `host_secrets.py sync` rewrites.
K8S_PLANE = "k8s"
HOST_PLANE = "host"

# The host packages (H5.7). A file under `hosts/components/<component>/` that names a secret
# makes every host package listing that component a consumer, on the host plane.
_COMPONENTS = os.path.join("hosts", "components")
# Directories whose hits are not consumers. `collections/` is vendored third-party; the
# registry and secrets file name every secret by definition, so including them would make
# every census non-empty and the check vacuous.
_CENSUS_SKIP = ("collections",)
# The manifests role's standalone-value guard map names about 150 keys and renders none of
# them: the guard reads each value at deploy time and writes nothing, so a rotation owes the
# `manifests` role no redeploy.
_CENSUS_SKIP_FILES = {
    "secrets.yml",
    "secret_rotation.yml",
    "standalone_secret_keys.json",
}

# Markdown is EXCLUDED, and this is a correctness fix rather than tidiness. A role's CLAUDE.md
# names secrets it explains without rendering any of them, so counting docs makes the census
# claim consumers that hold no copy — and sends the operator to redeploy a role that cannot
# help. Measured across all 149 secrets: 5 secrets gained 11 doc-only roles, and
# `r2_access_key_id` is the clearest — monitor-bridge's CLAUDE.md discusses it while the only
# renderers are the longhorn-r2-secret.yaml.j2 template and the k3s-host-checks output.
#
# Checked in the direction that matters before making the change: excluding docs creates ZERO
# new phantom tags, so no real consumer is only discoverable through prose.
_CENSUS_SKIP_SUFFIXES = (".md",)


@lru_cache(maxsize=None)
def _census_corpus(repo: str = REPO) -> tuple[tuple[str, str, str], ...]:
    """Every censusable k8s role and host component file, read once — (text, role, plane) each.

    The files are those under `ansible/roles/k8s/` and `hosts/components/`. A caller that asks about many secrets in one process matches each against the one corpus,
    a fraction of the I/O of walking ~1600 files per name.

    Only files that belong to a k8s role are kept from `ansible/`: a hit anywhere else cannot
    name a consumer, so carrying its text would cost memory for a match that is discarded.

    The corpus is cached for the process, which is correct for every caller here — the
    rotation tools and their tests read a tree that does not change under them. A caller that
    edits the tree mid-process must call `_census_corpus.cache_clear()`.
    """
    corpus: list[tuple[str, str, str]] = []
    ansible_dir = os.path.join(repo, "ansible")
    lookups = k8s_lookup_map(repo)
    for dirpath, dirnames, filenames in os.walk(ansible_dir):
        rel = os.path.relpath(dirpath, ansible_dir)
        if any(skip in rel for skip in _CENSUS_SKIP):
            dirnames[:] = []
            continue
        parts = rel.split(os.sep)
        if not (len(parts) >= 3 and parts[0] == "roles" and parts[1] == "k8s"):
            continue
        role = parts[2]
        for filename in filenames:
            if filename in _CENSUS_SKIP_FILES or filename.endswith(
                _CENSUS_SKIP_SUFFIXES
            ):
                continue
            try:
                with open(
                    os.path.join(dirpath, filename), encoding="utf-8", errors="ignore"
                ) as handle:
                    text = handle.read()
            except OSError:
                # A file this process cannot read is not evidence of absence, but it is also
                # not something a census can act on. Skipping is right; failing the whole
                # census on one unreadable file would make the tool useless in a worktree.
                continue
            corpus.append((text, role, K8S_PLANE))
            # A k8s role that `lookup()`s this file from another role renders what it holds,
            # so it is a consumer too (#3781).
            rel = os.path.relpath(os.path.join(dirpath, filename), ansible_dir)
            for reader in sorted(lookups.get(rel, ())):
                corpus.append((text, reader, K8S_PLANE))
    corpus.extend(_host_package_corpus(repo))
    return tuple(corpus)


def _packages_by_component(repo: str) -> dict[str, list[str]]:
    """Component name -> the host packages whose `package.yml` lists it, sorted by host."""
    listed: dict[str, list[str]] = {}
    hosts_dir = os.path.join(repo, "hosts")
    for host in sorted(os.listdir(hosts_dir)) if os.path.isdir(hosts_dir) else ():
        spec_path = os.path.join(hosts_dir, host, "package.yml")
        if not os.path.isfile(spec_path):
            continue
        with open(spec_path, encoding="utf-8") as handle:
            spec = yaml_fast.safe_load(handle.read())
        for component in (spec or {}).get("components") or []:
            listed.setdefault(str(component), []).append(host)
    return listed


def _host_package_corpus(repo: str) -> list[tuple[str, str, str]]:
    """(text, host package, `HOST_PLANE`) per file of `hosts/components/` and per package listing it.

    The second walk root of the census. The first covers `ansible/roles/`; H5.7 moved the first
    secret consumer, autoheal's webhook, out of it. A component no host package lists
    contributes nothing, because no package would carry its file.
    """
    out: list[tuple[str, str, str]] = []
    packages = _packages_by_component(repo)
    root = os.path.join(repo, _COMPONENTS)
    for dirpath, _dirnames, filenames in os.walk(root):
        component = os.path.relpath(dirpath, root).split(os.sep)[0]
        for host in packages.get(component, ()):
            for filename in filenames:
                if filename.endswith(_CENSUS_SKIP_SUFFIXES):
                    continue
                try:
                    with open(
                        os.path.join(dirpath, filename),
                        encoding="utf-8",
                        errors="ignore",
                    ) as handle:
                        out.append((handle.read(), host, HOST_PLANE))
                except OSError:
                    continue
    return out


def tree_consumers(name: str, repo: str = REPO) -> dict[str, str]:
    """Every role that REFERENCES this secret, measured from the tree — role -> plane.

    A grep in code rather than routing by name: name-routing once sent nine of 41
    `monitor_bridge_*` tokens to a role that renders them nowhere. It answers the question a
    rotation actually poses — "who now holds a stale copy?" — for every secret.

    Returns a plane per consumer because the repair command differs by plane. A host package
    that carries the secret through a component under `hosts/components/` is a consumer on
    `HOST_PLANE`, named by its host.
    """
    found: dict[str, str] = {}
    for text, role, plane in _census_corpus(repo):
        if name in text:
            found[role] = plane
    return found


# What reaches a consumer Flux applies (D3.4, d3-secrets.md, *Rotating a value read by both
# planes*). Flux substitutes the value from the cluster file, which only the operator's
# `write` regenerates, because it decrypts. Landing the PR then waits on `cluster-vars` and
# every Kustomization that depends on it. Backticked for the reason `consumer_commands` gives.
FLUX_PATH = (
    "%s is a k8s entry, so Flux applies it from the cluster file: the operator runs "
    "`uv run python scripts/secrets_mgmt/cluster_vars.py write` (operator-only), commits it "
    "with secrets.yml, and the PR lands with `./scripts/deploy_tools/land.sh`"
)


def consumer_commands(name: str, repo: str = REPO) -> list[str]:
    """The exact commands that make a rotated `name` take effect everywhere.

    Host packages are listed with the SOPS file they carry, and each k8s consumer with the Flux
    path, `FLUX_PATH`.
    """
    consumers = tree_consumers(name, repo=repo)
    k8s_roles = sorted(r for r, plane in consumers.items() if plane == K8S_PLANE)
    host_packages = sorted(r for r, plane in consumers.items() if plane == HOST_PLANE)
    # BACKTICKS ARE LOAD-BEARING, not decoration. This module is reached by a cron, and
    # `reference/scripts.py` classifies a script as `scheduled` when a scheduled script's
    # text invokes it — so a bare `scripts/secrets_mgmt/host_secrets.py ...` literal here would
    # reclassify that script as cron-driven in the generated reference. That resolver already
    # draws the distinction this needs (`_invoked_in`, reference/scripts.py:102): a line
    # carrying a backtick is prose CITING a command rather than running one, which is exactly
    # what these strings are. Quoting them says so in the one way the tree can read.
    commands = []
    for host in host_packages:
        commands.append(
            "`uv run python scripts/secrets_mgmt/host_secrets.py sync %s` (operator-only: "
            "it decrypts) rewrites the SOPS file that the package `hosts/%s/package.yml` "
            "carries" % (host, host)
        )
    commands.extend(FLUX_PATH % role for role in k8s_roles)
    return commands
