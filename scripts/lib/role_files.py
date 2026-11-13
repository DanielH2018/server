"""The paths a template passes to `lookup`, named from its role rather than from the tree's root.

`make_env` registers `role_file` and `repo_file` as globals, so every template and every role
default resolved through `resolve_vars` can call them. Each reads `role_path`, the directory
of the role being rendered, from the context. A test that renders a copy of a role sets
`role_path` to the copy, and every lookup then reads the copy. A static reader that resolves
a call without rendering it calls `role_file_path` or `repo_file_path`.

Typical usage example, in a template:

    {{ lookup('file', role_file('files/app.py')) }}
    {{ lookup('file', role_file('files/crowdsec-whitelist.yaml', role='crowdsec')) }}
    {{ lookup('file', repo_file('hosts/lib/host_lib.py')) }}
"""

from pathlib import Path

from jinja2 import UndefinedError, pass_context
from jinja2.runtime import Context, missing


def role_file_path(role_path: Path, path: str, role: str | None = None) -> Path:
    """`path` inside `role`, a sibling of the role at `role_path`, or inside that role for None."""
    return (role_path.parent / role if role else role_path) / path


def repo_file_path(role_path: Path, path: str) -> Path:
    """`path` from the repository root, two levels above the roles directory: `<root>/k8s/roles`."""
    return role_path.parents[2] / path


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


def _role_path(context: Context) -> Path:
    value = context.resolve_or_missing("role_path")
    if value is missing:
        raise UndefinedError(
            "role_file() and repo_file() read `role_path`, and this render has none: "
            "render the template through its role"
        )
    return Path(value)
