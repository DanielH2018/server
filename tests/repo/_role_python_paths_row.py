"""The `role-files-cite-real-python-paths` row of `test_census_rows_text.py`."""

import re
from functools import cache

from _row_table import Census, Subject, tracked

_PY_PATH = re.compile(r"(?<![\w/.-])((?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_-]+\.py)\b")


@cache
def _repo_paths() -> tuple[frozenset[str], frozenset[str]]:
    """Every tracked path, and every directory name that opens one or sits under its first."""
    paths = frozenset(tracked())
    return paths, frozenset(part for path in paths for part in path.split("/")[:2])


def _unresolved(subject: Subject) -> list[str]:
    paths, top_dirs = _repo_paths()
    return [
        f"`{cited}` matches no tracked file"
        for cited in _PY_PATH.findall(subject.text)
        if cited.split("/")[0] in top_dirs
        and not any(known == cited or known.endswith("/" + cited) for known in paths)
    ]


def _role_files() -> list[str]:
    return tracked("k8s/roles/*.j2", "k8s/roles/*.yml", "k8s/roles/*.md")


ROLE_FILES_CITE_REAL_PYTHON_PATHS = Census(
    name="role-files-cite-real-python-paths",
    reason=(
        "A role's comments and docs name the validator or script that enforces a rule. When "
        "that module moves, the text keeps the old path and sends a reader to nothing: six "
        "role comments named `validate/k8s_manifests.py` after it moved (#9371). A path "
        "resolves as a suffix of a tracked file, because role text writes "
        "`validate/render_lib/k8s.py` and `scripts/validate/render_lib/k8s.py` alike. A "
        "path after `/`, `.`, `-` or a word character is a host or image location, and one "
        "whose first directory is none of ours names another repository, so both are skipped."
    ),
    files=_role_files,
    offence=_unresolved,
    red=(
        Subject("k8s/roles/x/a.j2", "{# validate/k8s_manifests.py renders it #}"),
        Subject("k8s/roles/x/b.yml", "# scripts/validate/k8s_manifests.py fails it"),
    ),
    green=(
        Subject("k8s/roles/x/a.j2", "{# scripts/validate/render_lib/k8s.py #}"),
        Subject("k8s/roles/x/b.yml", "# validate/render_lib/k8s.py renders it"),
        Subject("k8s/roles/x/c.md", "runs /host/usr/lib/homelab/k3s_gates.py"),
        Subject("k8s/roles/x/d.md", "homeassistant/components/cast/discovery.py"),
    ),
    min_matches=100,
    must_find=frozenset({"k8s/roles/loki-homelab/templates/configmap.yaml.j2"}),
)
