"""Documentation reaches no host: which paths the deployer's mapper must classify as nothing.

A `.md` is prose about a role, never role content a playbook applies. The mapper tested the
suffix on the k8s arm and the container catch-all only, so a document under the setup plane or
under `roles/containers/common/` took a broad arm by path and routed prose to an apply
(issue #1714). The census at the bottom holds the suffix rule's own assumption — that no role
ships a `.md` as content — to the three READMEs that sit under a `files/` directory
(issue #1715).

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_changes_documents.py
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_changes_documents.py

import pathlib

import yaml

from deploy_changes import ChangeSet, services_from_changed_paths

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


def test_a_setup_roles_document_is_clean():
    """Prose under a setup role reaches no host, so it must classify as nothing at all.

    `ansible/roles/setup/<role>/CLAUDE.md` matches `_BROAD_SETUP_PREFIXES` by path, and the
    setup arm carried no docs test until issue #1714 — so a docs-only edit routed the deployer
    to an `initial_setup.yml --tags <role>` apply, or to a defer-and-alert for a role no
    playbook includes. `ansible/roles/containers/common/CLAUDE.md` is the same defect one plane
    over: it matches `_BROAD_DEPLOY_PREFIXES` and named a full `ansible/deploy.yml`.

    Paired with `test_a_setup_roles_config_is_still_broad` below: a docs test that swallowed
    the whole setup plane would pass here and fail there.
    """
    for path in (
        "ansible/roles/setup/k3s/CLAUDE.md",
        "ansible/roles/setup/gitops_deploy/CLAUDE.md",
        "ansible/roles/containers/common/CLAUDE.md",
    ):
        assert services_from_changed_paths([path]) == ChangeSet(), path


def test_a_setup_roles_config_is_still_broad():
    """The rejecting half: a real file under the same role still reaches the setup plane.

    Same directory as the CLAUDE.md above, so the two differ only in suffix — which is what
    makes this the proof that the docs test is a suffix test and not a prefix one.
    """
    cs = services_from_changed_paths(["ansible/roles/setup/k3s/defaults/main.yml"])
    assert cs.broad is True
    assert cs.broad_setup is True
    assert cs.setup_roles == {"k3s"}


def test_a_readme_under_a_roles_files_dir_ships_with_no_task():
    """The suffix rule assumes no role ships a `.md` as CONTENT. This holds it to that.

    Three READMEs sit under a k8s role's `files/` (issue #1715). Both roles copy a NAMED list
    of files — `home_assistant_automation_files` / `home_assistant_script_files` for the two
    under home-assistant, and an explicit `loop:` for configarr's — so none of them reaches a
    pod and classifying the path as undeployable is correct. A role that later copied its
    `files/` directory wholesale would break that, and this census is where it shows up.

    Non-vacuity is the frozenset, not a count: a rename or a move leaves the glob empty and an
    `all()` over nothing passes.
    """
    found = {
        str(p.relative_to(_REPO_ROOT))
        for p in (_REPO_ROOT / "ansible" / "roles" / "k8s").glob("*/files/**/*.md")
    }
    assert found == {
        "ansible/roles/k8s/configarr/files/baseline/README.md",
        "ansible/roles/k8s/home-assistant/files/automations/README.md",
        "ansible/roles/k8s/home-assistant/files/scripts/README.md",
    }
    for path in sorted(found):
        assert services_from_changed_paths([path]) == ChangeSet(), path
    # What "shipped by no task" means concretely, per role. home-assistant's ConfigMap carries
    # a NAMED list per subdirectory out of defaults/main.yml, so a file absent from the list
    # reaches no pod; configarr's `copy` loop names its sources literally and mentions no
    # README at all.
    ha_defaults = yaml.safe_load(
        (_REPO_ROOT / "ansible/roles/k8s/home-assistant/defaults/main.yml").read_text()
    )
    for key in (
        "home_assistant_root_files",
        "home_assistant_automation_files",
        "home_assistant_script_files",
        "home_assistant_template_files",
    ):
        shipped = ha_defaults[key]
        assert shipped, key
        assert [n for n in shipped if n.endswith(".md")] == [], key
    configarr_tasks = (
        _REPO_ROOT / "ansible/roles/k8s/configarr/tasks/main.yml"
    ).read_text()
    assert "README" not in configarr_tasks
