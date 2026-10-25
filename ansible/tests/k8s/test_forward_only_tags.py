"""The forward-only tags are the H1 plan's list, as far as the tree holds their roles (#5926).

The H1 plan's *Forward-only tags and the promote rule* names `metallb`, `coredns`, `longhorn`
and `longhorn-backup`, and H6.1b adds `system-upgrade` (the H6 plan's *SUC through Flux*, so a
failed Plan revision never lowers a Plan below a version a node runs). `promote_lib/forward_only.py` treats a role as forward-only when its
`defaults/main.yml` sets `flux_forward_only: true` on a top-level line, and a failed revision
that changes such a tag holds without resetting `prod`. A plan tag whose role ships without
the flag loses that hold silently, which is how H1.3 shipped `metallb` (#5926).

Run: uv run pytest ansible/tests/k8s/test_forward_only_tags.py
"""

import re

from _role_census import role_dirs
from deploy_tools.promote_lib.forward_only import FORWARD_ONLY_FLAG
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

PLAN_FORWARD_ONLY = frozenset(
    {"metallb", "coredns", "longhorn", "longhorn-backup", "system-upgrade"}
)
# forward_only_roles' `git grep` pre-filter: an indented or commented key never reaches it.
_FLAG_LINE = re.compile(rf"^{FORWARD_ONLY_FLAG}:", re.MULTILINE)


def _drift(roles_dir):
    """`(missing, extra)` between the plan's forward-only tags and the roles that set the flag.

    `missing` is each plan tag with a role under `roles_dir` that does not set the flag the way
    `forward_only_roles` reads it. `extra` is each flagged role the plan does not name.
    """
    flagged = set()
    for defaults in roles_dir.glob("*/defaults/main.yml"):
        text = defaults.read_text()
        if (
            _FLAG_LINE.search(text)
            and (yaml_fast.safe_load(text) or {}).get(FORWARD_ONLY_FLAG) is True
        ):
            flagged.add(defaults.parent.parent.name)
    present = {p.name for p in role_dirs(roles_dir)}
    return sorted((PLAN_FORWARD_ONLY & present) - flagged), sorted(
        flagged - PLAN_FORWARD_ONLY
    )


def _role(roles_dir, name, text):
    (roles_dir / name / "defaults").mkdir(parents=True)
    (roles_dir / name / "defaults" / "main.yml").write_text(text)


def test_a_plan_role_without_the_flag_is_missing_and_an_unplanned_flag_is_extra(
    tmp_path,
):
    _role(tmp_path, "metallb", "flux_prune: false\n")
    _role(tmp_path, "longhorn", "flux_forward_only: 'true'\n")
    _role(tmp_path, "sonarr", "flux_forward_only: true\n")
    assert _drift(tmp_path) == (["longhorn", "metallb"], ["sonarr"])


def test_the_forward_only_roles_are_the_plans_tags_and_name_metallb():
    assert _drift(K8S_ROLES) == ([], [])
    # The named member: without it, a plan whose roles all left the tree passes empty.
    assert (K8S_ROLES / "metallb" / "defaults" / "main.yml").is_file()
