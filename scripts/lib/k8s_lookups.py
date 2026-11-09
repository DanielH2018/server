"""The k8s files another k8s role `lookup()`s, mapped to the roles that read them.

`scripts/secrets_mgmt/consumers.py` asks this of the whole tree, so a secret a reader renders
out of another role's file credits that reader too. `narrow_lib/secret_keys.py` asks it of a
checkout to narrow a secrets change.
"""

import re
from pathlib import Path

# A `lookup('file' | 'template', playbook_dir ~ '/roles/k8s/<role>/<path>')` naming one file,
# with `+` or `~` as the join and any keyword arguments after the path (`rstrip=False`, #5463).
# Group 1 is the path from `roles/`. A lookup that builds its path from a variable
# (`'/roles/k8s/x/files/' + name`) names a directory, never a file, and every such lookup in
# the tree reads its own role.
K8S_LOOKUP_RE = re.compile(
    r"lookup\(\s*'(?:ansible\.builtin\.)?(?:file|template)'\s*,\s*playbook_dir\s*[+~]\s*"
    r"'/(roles/k8s/[^']+)'\s*(?:,[^)]*)?\)"
)


def k8s_lookup_map(repo_root) -> dict[str, set[str]]:
    """Every k8s file another role `lookup()`s, from `roles/`, mapped to the roles reading it.

    A role reading its own file is left out.
    """
    k8s = Path(repo_root) / "ansible" / "roles" / "k8s"
    found: dict[str, set[str]] = {}
    if not k8s.is_dir():
        return found
    for role in sorted(k8s.iterdir()):
        for src in [*role.glob("templates/**/*.j2"), *role.glob("tasks/*.yml")]:
            try:
                text = src.read_text(errors="ignore")
            except OSError:
                continue
            for target in K8S_LOOKUP_RE.findall(text):
                if target.split("/")[2] != role.name:
                    found.setdefault(target, set()).add(role.name)
    return found
