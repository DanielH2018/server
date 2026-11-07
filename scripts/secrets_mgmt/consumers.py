"""Who holds a copy of a secret, by two deliberately different mechanisms.

`consumer_tags()` routes by NAME. It is fast, it covers the push-token fleet, and it is what
the unattended weekly rotation acts on: a token with no tags is MANUAL and the cron skips it.

`tree_consumers()` measures the same question from the tree with a grep. It answers "who is
still holding the old value?" for every secret, including the ones name-routing has no rule
for — the question a rotation actually poses, and the one `audit` does not answer.

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
from lib.repo_paths import GITOPS_DEPLOY_FILES

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deployer_lib.deploy_cross_role import k8s_lookup_map


def consumer_tags(name: str) -> tuple[str, ...]:
    """Deploy tags whose redeploy makes a rotated push token take effect.

    EMPTY when the consumer spans hosts or nothing reads the token: those stay MANUAL. The
    unattended cron skips them, and the audit still reminds.
    """
    if name.startswith("monitor_bridge_"):
        return ("monitor-bridge",)
    # anything else unrecognised -> manual
    return ()


# Planes a role can live on, and what redeploying it actually costs the operator. The
# distinction is not cosmetic: `deploy.sh` derives its valid tags from `containers_list`, so a
# setup-plane role HAS no deploy tag and `deploy.sh --tags fake_remux` exits 2 having deployed
# nothing (`scripts/lib/exit_codes.py`, `DEPLOY_TAG_MISS`). That is the trap this census exists
# to surface.
_ROLE_PLANES = {
    "k8s": "deploy",
    "containers": "deploy",
    "setup": "setup",
}

# Directories whose hits are not consumers. `collections/` is vendored third-party; the
# registry and secrets file name every secret by definition, so including them would make
# every census non-empty and the check vacuous.
# The host packages (H5.7). A file under `hosts/components/<component>/` that names a secret
# makes every host package listing that component a consumer, on this plane. A host package is
# not a role: `deploy.sh` cannot reach it, and the host's SOPS file carries the value, which
# only the operator's `host_secrets.py sync` rewrites.
HOST_PLANE = "host"
_COMPONENTS = os.path.join("hosts", "components")
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
# renderers are setup/k3s's longhorn-r2-secret.yaml.j2 and health-crons.yml.
#
# Checked in the direction that matters before making the change: excluding docs creates ZERO
# new phantom tags, so no real consumer is only discoverable through prose.
_CENSUS_SKIP_SUFFIXES = (".md",)


@lru_cache(maxsize=None)
def _census_corpus(repo: str = REPO) -> tuple[tuple[str, str, str], ...]:
    """Every censusable file under `ansible/`, read once — (text, role, plane) per file.

    The two parametrized tests in `ansible/tests/k8s/test_secret_consumer_census.py` run over
    all ~180 names in `sops_names()`. Reading the tree once and matching every name against the
    one corpus costs a fraction of the I/O of walking ~1600 files per name.

    Only files that belong to a role on a known plane are kept: a hit anywhere else cannot
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
        if not (len(parts) >= 3 and parts[0] == "roles" and parts[1] in _ROLE_PLANES):
            continue
        role, plane = parts[2], _ROLE_PLANES[parts[1]]
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
            corpus.append((text, role, plane))
            # A k8s role that `lookup()`s this file from another role renders what it holds,
            # so it is a consumer too (#3781).
            rel = os.path.relpath(os.path.join(dirpath, filename), ansible_dir)
            for reader in sorted(lookups.get(rel, ())):
                corpus.append((text, reader, _ROLE_PLANES["k8s"]))
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

    The counterpart to `consumer_tags()` above, and deliberately a different mechanism.
    `consumer_tags()` routes by NAME, which is fast and covers the push-token fleet, but its
    own comment records what name-routing costs: nine of 41 `monitor_bridge_*` tokens named a
    role that renders them nowhere, and the fix was measured with `grep -rl <token>
    ansible/roles/` rather than reasoned from the prefix. This does that grep in code so the
    measurement is repeatable and can be asserted against.

    It answers the question a rotation actually poses — "who now holds a stale copy?" — which
    `consumer_tags()` cannot for anything outside its table. `sonarr_api_key` falls to that
    function's default and returns `()`, meaning MANUAL: correct, but it names nobody, so its
    consumers can hold a dead key until someone finds them.

    Returns a plane per role because the repair command differs by plane and one of them is
    unreachable from `deploy.sh` — see `_ROLE_PLANES`. A host package that carries the secret
    through a component under `hosts/components/` is a consumer on `HOST_PLANE`, named by its
    host.
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
    "%s carries owner: flux, so Flux applies it from the cluster file: the operator runs "
    "`uv run python scripts/secrets_mgmt/cluster_vars.py write` (operator-only), commits it "
    "with secrets.yml, and the PR lands with `./scripts/deploy_tools/land.sh`"
)


def flux_applies_line(tag: str) -> str:
    """The line `rotate --commit` prints for each consumer of a rotated secret.

    `rotate --commit` has already run `write` in process (`sync_cluster_files`), so the rest
    of the Flux path is the commit landing.
    """
    return (
        "  not redeployed: %s carries owner: flux; Flux applies it once the commit "
        "carrying the cluster files lands" % tag
    )


def consumer_commands(name: str, repo: str = REPO) -> list[str]:
    """The exact commands that make a rotated `name` take effect everywhere.

    Setup-plane roles are listed with their own playbook, host packages with the SOPS file they
    carry, and each k8s consumer with the Flux path, `FLUX_PATH`.
    """
    consumers = tree_consumers(name, repo=repo)
    deploy_roles = sorted(r for r, plane in consumers.items() if plane == "deploy")
    setup_roles = sorted(r for r, plane in consumers.items() if plane == "setup")
    host_packages = sorted(r for r, plane in consumers.items() if plane == HOST_PLANE)
    # BACKTICKS ARE LOAD-BEARING, not decoration. This module is reached by two crons, and
    # `reference/scripts.py` classifies a script as `scheduled` when a scheduled script's
    # text invokes it — so a bare `scripts/secrets_mgmt/host_secrets.py ...` literal here would
    # reclassify that script as cron-driven in the generated reference. That resolver already
    # draws the distinction this needs (`_invoked_in`, reference/scripts.py:102): a line
    # carrying a backtick is prose CITING a command rather than running one, which is exactly
    # what these strings are. Quoting them says so in the one way the tree can read.
    commands = []
    for role in setup_roles:
        commands.append(
            "`uv run ansible-playbook ansible/initial_setup.yml --tags %s`" % role
        )
    for host in host_packages:
        commands.append(
            "`uv run python scripts/secrets_mgmt/host_secrets.py sync %s` (operator-only: "
            "it decrypts) rewrites the SOPS file that the package `hosts/%s/package.yml` "
            "carries" % (host, host)
        )
    commands.extend(FLUX_PATH % tag for tag in deploy_roles)
    return commands
