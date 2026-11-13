"""The k8s files another k8s role `lookup()`s, mapped to the roles that read them.

`scripts/secrets_mgmt/consumers.py` asks this of the whole tree, so a secret a reader renders
out of another role's file credits that reader too. `narrow_lib/secret_keys.py` asks it of a
checkout to narrow a secrets change.
"""

import re
from pathlib import Path

# A `role_file('<path>', role='<role>')` naming one file of another role, the form every
# cross-role lookup passes its path in. Group 1 is the path, group 2 the role. A path built
# from a variable (`'files/' + name`) is not matched, and every such path in the tree names
# its own role's file.
_CROSS_ROLE_FILE = re.compile(
    r"role_file\(\s*'([^']+)'\s*,\s*role\s*=\s*'([^']+)'\s*\)"
)


def cross_role_files(text: str) -> list[str]:
    """Each other role's file `text` names through `role_file`, as `k8s/roles/<role>/<path>`."""
    return [f"k8s/roles/{role}/{path}" for path, role in _CROSS_ROLE_FILE.findall(text)]


def k8s_lookup_map(repo_root) -> dict[str, set[str]]:
    """Every k8s file another role `lookup()`s, from `roles/`, mapped to the roles reading it.

    A role reading its own file is left out.
    """
    k8s = Path(repo_root) / "k8s" / "roles"
    found: dict[str, set[str]] = {}
    if not k8s.is_dir():
        return found
    for role in sorted(k8s.iterdir()):
        for src in role.glob("templates/**/*.j2"):
            try:
                text = src.read_text(errors="ignore")
            except OSError:
                continue
            for target in cross_role_files(text):
                if target.split("/")[2] != role.name:
                    found.setdefault(target, set()).add(role.name)
    return found
