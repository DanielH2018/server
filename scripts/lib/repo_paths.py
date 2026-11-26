"""The repo path anchors a script under ``scripts/`` reads the tree through.

WHY A MODULE WITH NO IMPORTS. ``render_guard.py`` was meant to be the one definition of
these, but importing it costs a ``jinja2`` and a ``yaml`` import, so a script that only
needed a path kept computing ``Path(__file__).resolve().parents[2]`` itself -- twenty of
them by 2026-09-01, under three different names (``REPO``, ``REPO_ROOT``, ``_REPO``). This
module imports nothing, so there is no reason left to inline the arithmetic.

Every path is absolute and resolved, so a script run from any cwd -- a cron, a prek hook,
a worktree under ``.claude/worktrees/`` -- reads the checkout it lives in, never the one
the shell happens to be in. That is the property a landing relies on when it runs
from the worktree it was invoked from.

Import it from the installed root, as any other ``lib`` module::

    from lib.repo_paths import REPO, HOST_VARS

A test that needs to point a reader at a ``tmp_path`` passes the path as an argument; these
constants are defaults, never the only way in.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def primary_checkout_of(repo: Path) -> Path:
    """The checkout the worktrees hang off, read from ``repo``'s ``.git`` file.

    A linked worktree's ``.git`` is a file reading ``gitdir: <primary>/.git/worktrees/<name>``,
    and the primary is three parents up. Any other shape (a primary checkout, a separated
    metadata directory, no ``.git`` at all) answers ``repo`` itself. It starts no subprocess,
    so a module that needs the path at import time pays one file read.
    """
    dotgit = repo / ".git"
    try:
        text = dotgit.read_text(encoding="utf-8").strip() if dotgit.is_file() else ""
    except OSError:
        return repo
    if not text.startswith("gitdir:"):
        return repo
    gitdir = repo / text.removeprefix("gitdir:").strip()
    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
        return gitdir.parent.parent.parent
    return repo


# The checkout commands run in even when the script lives in a worktree under it.
PRIMARY_CHECKOUT = primary_checkout_of(REPO)
SCRIPTS = REPO / "scripts"
DOCS = REPO / "docs"

K8S = REPO / "k8s"
K8S_ROLES = K8S / "roles"
JOB_FILES = SCRIPTS / "renderer" / "job_files"
# The one copy of longhorn_robustness.py lives here without shipping in the pod; runbook gates
# import it.
MONITOR_BRIDGE_FILES = K8S_ROLES / "monitor-bridge" / "files"
# A tool under scripts/ importing host_lib or ci_verdict puts this on sys.path first.
HOST_LIB_FILES = REPO / "hosts" / "lib"
# The GitHub spend ledger, `gh_quota.py`, which `lib.gh` records each call into.
GH_QUOTA_FILES = REPO / "hosts" / "components" / "gh-quota" / "files"
SHARED_TPL = K8S / "templates"  # shared macros

# The SOPS files, their creation rules and the plaintext rotation register.
SECRETS = REPO / "secrets"
# Each cluster key's ciphertext digest, which `checksum/restart` and the verify Jobs' roll hash.
CLUSTER_VARS_SOURCE = SECRETS / "cluster-vars.source"
# ADR-0042's shared layers: the estate's facts, the cluster's, and the defaults every host shares.
ESTATE_VARS = REPO / "estate" / "vars.yml"
K8S_VARS = K8S / "vars.yml"
# Each host's own values, `hosts/<host>/vars.yml`; `inventory_lists.host_vars_file` names one.
HOST_VARS = REPO / "hosts"
HOSTS_SHARED_VARS = HOST_VARS / "vars.yml"
# The k3s service list, every template's `services` (render_guard.k8s_services).
K8S_SERVICES = REPO / "k8s" / "services.yml"
# Node keys both k3s components render; the Longhorn backup schedule and arm flags are
# LONGHORN_BACKUP_ROLE's.
K3S_SERVER = REPO / "hosts" / "components" / "k3s-server"
# Python readers resolve the three Longhorn tier lists through `lib.service_tiers`.
LONGHORN_RECONCILE_ROLE = K8S_ROLES / "longhorn-reconcile"
LONGHORN_BACKUP_ROLE = K8S_ROLES / "longhorn-backup"
K3S_DEFAULTS = K3S_SERVER / "defaults" / "main.yml"
# The k3s pin keys live in system-upgrade's defaults; K3S_DEFAULTS keeps every other key.
SYSTEM_UPGRADE_DEFAULTS = K8S_ROLES / "system-upgrade" / "defaults" / "main.yml"
# The host checks' schedules and thresholds are in its defaults/main.yml; files/ holds
# longhorn_backup_health.py.
K3S_HOST_CHECKS = REPO / "hosts" / "components" / "k3s-host-checks"
K3S_HOST_CHECKS_DEFAULTS = K3S_HOST_CHECKS / "defaults" / "main.yml"
K3S_HOST_DNS = REPO / "hosts" / "components" / "k3s-host-dns"
K3S_FILES = K3S_HOST_CHECKS / "files"
# The suite's shared guard modules (`_ratchet.py`, `_helpers.py`), for a script that reuses one.
TESTS = REPO / "tests"

# A script outside the roles importing from a role directory must be listed here, or
# stdlib_only.py's walk refuses it.
ROLE_MODULES = {
    MONITOR_BRIDGE_FILES: ["longhorn_robustness"],
}
