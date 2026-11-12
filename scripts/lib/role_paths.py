"""Which role directory a changed path sits in, and which deploy tag it maps to.

`land.sh` reads a PR's file list through these to derive the tags it gates, and
`findings_lib/precomputed.py` reads them to say what a brief's cited paths deploy.

Run: uv run pytest scripts/lib/tests/test_role_paths.py
"""

import re
from typing import NamedTuple

# The second group takes a TRAILING SLASH, so a file at a role's own root reports no
# subdirectory rather than its own basename.
_ROLE_PATH = re.compile(r"^ansible/roles/k8s/([^/]+)/(?:([^/]+)/)?")

SECRETS_FILE = "ansible/vars/secrets.yml"


class RolePath(NamedTuple):
    """The k8s role directory a changed path sits in.

    Attributes:
        role: the role directory's name. Not necessarily a deploy tag: only a role with a
            `containers_list` entry has one, which is `tag_for`'s question.
        subdir: the directory under the role the path sits in (`templates`, `tasks`, `files`),
            or `""` for a file at the role's own root.
    """

    role: str
    subdir: str


def role_of(path: str) -> RolePath | None:
    """Which k8s role directory `path` belongs to, or None when it belongs to none."""
    m = _ROLE_PATH.match(path)
    return RolePath(m.group(1), m.group(2) or "") if m else None


def is_test_path(path: str) -> bool:
    """Whether a changed path is test-suite material that no role ships anywhere.

    The `tests/` directory shape, which `test_testpaths_covers_every_test_file.py` enforces,
    plus a `test_*.py` or `conftest.py` wherever it sits. The `no-role-ships-a-pytest-file`
    row of `ansible/tests/repo/test_census_rows_roles.py` holds that no role ships one.
    """
    if path.startswith("ansible/tests/"):
        return True
    parts = path.split("/")
    if "tests" in parts[:-1]:
        return True
    name = parts[-1]
    return name == "conftest.py" or (name.startswith("test_") and name.endswith(".py"))


def role_for(path: str) -> str | None:
    """The k8s role directory a changed path belongs to, or None.

    A `.md` belongs to no role: a document is nothing a render reads, and the
    `no-role-ships-a-markdown-file` row of `test_census_rows_roles.py` keeps that true.
    """
    if path.endswith(".md"):
        return None
    at = role_of(path)
    return at.role if at else None


def tag_for(path: str, declared: set[str]) -> str | None:
    """The deploy tag a changed path maps to, or None.

    A role's own tests map to no tag, because a pytest guard reaches no cluster.
    """
    role = role_for(path)
    if role is None or is_test_path(path):
        return None
    return role if role in declared else None
