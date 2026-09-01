"""The repo path anchors a script under ``scripts/`` reads the Ansible tree through.

WHY A MODULE WITH NO IMPORTS. ``render_guard.py`` was meant to be the one definition of
these, but importing it costs a ``jinja2`` and a ``yaml`` import, so a script that only
needed a path kept computing ``Path(__file__).resolve().parents[2]`` itself -- twenty of
them by 2026-09-01, under three different names (``REPO``, ``REPO_ROOT``, ``_REPO``). This
module imports nothing, so there is no reason left to inline the arithmetic.

Every path is absolute and resolved, so a script run from any cwd -- a cron, a prek hook,
a worktree under ``.claude/worktrees/`` -- reads the checkout it lives in, never the one
the shell happens to be in. That is the property ``deploy.sh`` relies on when it renders
from the worktree it was invoked from.

Import it through the same bootstrap as any other ``lib`` module::

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from lib.repo_paths import REPO, HOST_VARS  # noqa: E402

A test that needs to point a reader at a ``tmp_path`` passes the path as an argument; these
constants are defaults, never the only way in.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
DOCS = REPO / "docs"

ANSIBLE = REPO / "ansible"
ROLES = ANSIBLE / "roles"
K8S_ROLES = ROLES / "k8s"
SHARED_TPL = (
    ANSIBLE / "templates"
)  # shared macros (and the labels-macro traefik.yml.j2)

INVENTORY = ANSIBLE / "inventory"
HOSTS_INI = INVENTORY / "hosts.ini"
ALL_VARS = INVENTORY / "group_vars" / "all.yml"
HOST_VARS = INVENTORY / "host_vars"
