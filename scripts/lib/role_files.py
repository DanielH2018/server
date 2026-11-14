"""The paths a template passes to `lookup`, named from its role rather than from the tree's root.

`make_env` registers `role_file`, `repo_file` and `cluster_vars_source` as globals, so every template and every role
default resolved through `resolve_vars` can call them. Each reads `role_path`, the directory
of the role being rendered, from the context. A test that renders a copy of a role sets
`role_path` to the copy, and every lookup then reads the copy. A static reader that resolves
a call without rendering it calls `role_file_path`, `repo_file_path` or `cluster_vars_source_path`.

Typical usage example, in a template:

    {{ lookup('file', role_file('files/app.py')) }}
    {{ lookup('file', role_file('files/crowdsec-whitelist.yaml', role='crowdsec')) }}
    {{ lookup('file', repo_file('hosts/lib/host_lib.py')) }}
    {{ lookup('file', cluster_vars_source()) }}
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from pathlib import Path

from jinja2 import UndefinedError, pass_context
from jinja2.runtime import Context, missing

from lib.repo_paths import CLUSTER_VARS_SOURCE, K8S_ROLES, REPO

_ROLES_DEPTH = len(K8S_ROLES.relative_to(REPO).parts)


def role_file_path(role_path: Path, path: str, role: str | None = None) -> Path:
    """`path` inside `role`, a sibling of the role at `role_path`, or inside that role for None."""
    return (role_path.parent / role if role else role_path) / path


def repo_file_path(role_path: Path, path: str | Path) -> Path:
    """`path` from the repository root, which holds the roles directory where `K8S_ROLES` does."""
    return role_path.parents[_ROLES_DEPTH] / path


def cluster_vars_source_path(role_path: Path) -> Path:
    """`cluster-vars.source` in the repository holding the role at `role_path`."""
    return repo_file_path(role_path, CLUSTER_VARS_SOURCE.relative_to(REPO))


@pass_context
def role_file(context: Context, path: str, role: str | None = None) -> str:
    """The absolute path of `path` inside a role's directory.

    Args:
        path: The path from the role directory, such as `files/app.py`.
        role: The role holding it, a sibling of the one rendering. None for the rendering role.

    Raises:
        UndefinedError: The context holds no `role_path`.
    """
    return str(role_file_path(_role_path(context), path, role))


@pass_context
def repo_file(context: Context, path: str) -> str:
    """The absolute path of `path` from the repository root, for a file no role holds.

    Raises:
        UndefinedError: The context holds no `role_path`.
    """
    return str(repo_file_path(_role_path(context), path))


@pass_context
def cluster_vars_source(context: Context) -> str:
    """The absolute path of `cluster-vars.source`, each cluster key's ciphertext digest.

    Raises:
        UndefinedError: The context holds no `role_path`.
    """
    return str(cluster_vars_source_path(_role_path(context)))


def _role_path(context: Context) -> Path:
    value = context.resolve_or_missing("role_path")
    if value is missing:
        raise UndefinedError(
            "role_file(), repo_file() and cluster_vars_source() read `role_path`, and this render has none: "
            "render the template through its role"
        )
    return Path(value)
