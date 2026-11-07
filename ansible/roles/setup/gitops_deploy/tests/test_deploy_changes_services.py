"""Reading a git diff into "which services must be redeployed".

Getting this wrong is silent in both directions. Too narrow and a changed template never
reaches the host while the tick reports success; too broad and every push redeploys the estate.
A k8s role dir shares one channel, docs stay silent, and the setup plane goes broad.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_changes_services.py

import pytest

from deployer_lib import deploy_cross_role
from deployer_lib.deploy_changes import (
    role_of,
    services_from_changed_paths,
    setup_roles_for,
    setup_tags_for,
)


def test_unrelated_path_ignored():
    paths = ["docs/superpowers/specs/x.md", "README.md"]
    cs = services_from_changed_paths(paths)
    assert cs.flux == set()
    assert cs.broad is False


# A secrets-only push (e.g. a manual rotation of an assisted/external secret from another
# machine) maps to no service and isn't broad, so it would fall into the silent
# `git merge --ff-only; return` path — the rotated value then sits stale in the running
# container with no redeploy and no alert. It must instead be flagged so the deployer
# defers-and-alerts. (Adding ansible/vars/ to _BROAD_PREFIXES was rejected: that would also
# force the /add-secret flow — secrets.yml + the consuming template together — into a manual
# full deploy instead of the correct single-service deploy.)
def test_secrets_only_change_flags_secrets_not_broad():
    cs = services_from_changed_paths(["ansible/vars/secrets.yml"])
    assert cs.secrets is True
    assert cs.flux == set()
    assert cs.broad is False


def test_secret_rotation_registry_only_is_not_secrets():
    # The plaintext registry (names/dates, no value change) needs no redeploy — a silent
    # ff is correct, so it must NOT trip the secrets flag.
    cs = services_from_changed_paths(["ansible/secret_rotation.yml"])
    assert cs.secrets is False
    assert cs.flux == set()
    assert cs.broad is False


# k8s roles (ansible/roles/k8s/<role>/...) once matched NONE of the role regexes (all
# containers/-scoped), so services_from_changed_paths returned an EMPTY ChangeSet and main()
# took it as a plain docs-only ff-merge — silent, on every has_gitops host. Maps to cs.flux:
# this deployer never auto-deploys a k8s role's manifest change.
def test_k8s_role_change_flags_flux():
    cs = services_from_changed_paths(
        ["ansible/roles/k8s/authelia/templates/deployment.yaml.j2"]
    )
    assert cs.flux == {"authelia"}
    assert cs.broad is False


def test_k8s_role_matches_any_subdir_not_just_templates():
    # Unlike containers/ (split into templates/tasks/meta channels), a k8s role has no separate
    # auto-deploy path for any of its subdirs to scope against — the whole role dir shares one
    # channel.
    cs = services_from_changed_paths(["ansible/roles/k8s/authelia/tasks/main.yml"])
    assert cs.flux == {"authelia"}


def test_k8s_role_docs_stay_silent():
    cs = services_from_changed_paths(["ansible/roles/k8s/authelia/CLAUDE.md"])
    assert cs.flux == set()
    assert cs.broad is False


# ── role_of: the one answer to "which role directory is this path in" ──────────────
@pytest.mark.parametrize(
    "path,expected",
    [
        ("ansible/roles/k8s/sonarr/defaults/main.yml", ("k8s", "sonarr", "defaults")),
        ("ansible/roles/setup/k3s/tasks/server.yml", ("setup", "k3s", "tasks")),
        # A file at the role's own root reports NO subdirectory rather than its own basename.
        # The catch-all branch of `services_from_changed_paths` reads that field.
        ("ansible/roles/k8s/sonarr/README", ("k8s", "sonarr", "")),
    ],
)
def test_role_of_names_the_plane_the_role_and_the_subdirectory(path, expected):
    assert role_of(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "docs/gitops-pipeline.md",
        "ansible/deploy.yml",
        "ansible/templates/traefik.yml.j2",
        # A role TREE with no role under it: two segments, not three.
        "ansible/roles/k8s/",
        # A tree this deployer knows nothing about.
        "ansible/roles/staging/thing/tasks/main.yml",
        # The Pi's Docker role tree, which H5.12 deleted: no plane applies a path there.
        "ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2",
    ],
)
def test_role_of_is_none_for_a_path_in_no_role_directory(path):
    assert role_of(path) is None


def test_a_file_other_roles_ship_reaches_every_role_shipping_it():
    """deploy_ui installs the deployer's `gitops_markers.py` by path (#3306), so it re-applies
    beside its owner. Another deployer module does not reach it."""
    markers = ["ansible/roles/setup/gitops_deploy/files/gitops_markers.py"]
    roles = {"gitops_deploy", "deploy_ui"}
    assert services_from_changed_paths(markers).setup_roles == roles
    assert setup_tags_for(markers) == roles
    other = ["ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_logic.py"]
    assert services_from_changed_paths(other).setup_roles == {"gitops_deploy"}
    assert setup_tags_for(other) == {"gitops_deploy"}


def test_a_host_lib_change_reaches_its_consumers_and_not_common():
    """`common` reaches a host only through the roles copying `host_lib.py` (#3312). The five
    initial_setup consumers apply on the tick, and `common` is named nowhere. A `common` file no role ships still names `common`."""
    host_lib = ["hosts/lib/host_lib.py"]
    consumers = {
        "deploy_ui",
        "gitops_deploy",
    }
    assert services_from_changed_paths(host_lib).setup_roles == consumers
    assert setup_tags_for(host_lib) == consumers
    resolv = ["ansible/roles/setup/common/templates/resolv.conf.j2"]
    assert services_from_changed_paths(resolv).setup_roles == {"common"}


def test_a_table_holding_only_the_old_host_lib_key_routes_the_moved_file_nowhere():
    """The red half of the move (H7.2e): a table that kept the pre-move key finds no consumer
    for `hosts/lib/host_lib.py`, so the row must be repointed with the file."""
    live = deploy_cross_role.current_tables()
    old = {"ansible/roles/setup/common/files/host_lib.py": frozenset({"k3s"})}
    deploy_cross_role.use_tables({**live, "SETUP_FILES_SHIPPED_BY_OTHER_ROLES": old})
    try:
        assert (
            services_from_changed_paths(["hosts/lib/host_lib.py"]).setup_roles == set()
        )
    finally:
        deploy_cross_role.use_tables(live)


def test_a_common_task_file_change_reaches_the_roles_importing_it():
    """An `import_tasks` of a `common/tasks/` file is static, so the imported tasks run under
    each importer's tags and only re-applying the importers applies the change (#3317). The
    same five initial_setup roles apply on the tick."""
    path = "ansible/roles/setup/common/tasks/install_host_lib.yml"
    consumers = {
        "deploy_ui",
        "gitops_deploy",
    }
    assert setup_roles_for(path) == consumers
    assert setup_tags_for([path]) == consumers


def test_a_check_timer_template_change_reaches_the_roles_importing_its_task_file():
    """`check_timer.yml` renders the kuma-check pair, so a template edit reaches a host
    only through that task file's importers (#3319). `common` is not recorded, so the alert
    does not print the resolv.conf remediation for it."""
    path = "ansible/roles/setup/common/templates/check-timer.timer.j2"
    consumers = {"gitops_deploy", "initial_setup"}
    assert setup_roles_for(path) == consumers
    assert setup_tags_for([path]) == consumers


def test_an_initial_setup_defaults_change_reaches_only_its_own_role():
    path = "ansible/roles/setup/initial_setup/defaults/main.yml"
    assert services_from_changed_paths([path]).setup_roles == {"initial_setup"}
    assert setup_roles_for(path) == {"initial_setup"}
    assert setup_tags_for([path]) == {"initial_setup"}
    tasks = "ansible/roles/setup/initial_setup/tasks/crons.yml"
    assert setup_roles_for(tasks) == {"initial_setup"}
