"""Reading a git diff into "which services must be redeployed".

Getting this wrong is silent in both directions. Too narrow and a changed template never
reaches the host while the tick reports success; too broad and every push redeploys the estate.
Each Docker role has separate channels (the compose or a config template deploys, `tasks/` and
`meta/` defer-and-alert, docs stay silent), and a k8s role dir shares one channel.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_changes_services.py

import pytest

from deploy_changes import (
    role_of,
    services_from_changed_paths,
    setup_roles_for,
    setup_tags_for,
)


def test_single_service_template():
    paths = ["ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2"]
    cs = services_from_changed_paths(paths)
    assert cs.services == {"cadvisor"}
    assert cs.broad is False


def test_multiple_services():
    paths = [
        "ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2",
        "ansible/roles/containers/couchdb/templates/docker-compose.yml.j2",
    ]
    cs = services_from_changed_paths(paths)
    assert cs.services == {"cadvisor", "couchdb"}
    assert cs.broad is False


def test_unrelated_path_ignored():
    paths = ["docs/superpowers/specs/x.md", "README.md"]
    cs = services_from_changed_paths(paths)
    assert cs.services == set()
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
    assert cs.services == set()
    assert cs.broad is False


def test_secrets_with_service_template_still_deploys_that_service():
    # The /add-secret flow commits secrets.yml + the consuming template together — the
    # service maps, so it deploys normally (applying the secret); the flag is also set.
    cs = services_from_changed_paths(
        [
            "ansible/vars/secrets.yml",
            "ansible/roles/containers/karakeep/templates/docker-compose.yml.j2",
        ]
    )
    assert cs.services == {"karakeep"}
    assert cs.secrets is True
    assert cs.broad is False


def test_no_secrets_change_leaves_flag_false():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2"]
    )
    assert cs.secrets is False


def test_secret_rotation_registry_only_is_not_secrets():
    # The plaintext registry (names/dates, no value change) needs no redeploy — a silent
    # ff is correct, so it must NOT trip the secrets flag.
    cs = services_from_changed_paths(["ansible/secret_rotation.yml"])
    assert cs.secrets is False
    assert cs.services == set()
    assert cs.broad is False


# A service-scoped change to a bind-mounted CONFIG template or files/ asset (not just the
# compose) must map to that service for a scoped, health-gated redeploy — closing the GitOps loop
# so live config matches master. Otherwise these fall into the silent ff-merge "docs-only" path
# (the config sits stale in the running container with no redeploy and no alert).
def test_config_template_change_maps_to_service():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/prometheus/templates/prometheus.yml.j2"]
    )
    assert cs.services == {"prometheus"}
    assert cs.broad is False


def test_files_asset_change_maps_to_service():
    # Must be a DOCKER role path: k8s paths defer-and-alert instead of mapping to a
    # service. The Pi's services are the only Docker ones. The asset name is illustrative -- this parses paths, it does
    # not stat them.
    cs = services_from_changed_paths(
        ["ansible/roles/containers/glances/files/glances.conf"]
    )
    assert cs.services == {"glances"}
    assert cs.broad is False


@pytest.mark.parametrize(
    "path",
    [
        "ansible/roles/containers/common/templates/healthcheck.yml.j2",
        "ansible/roles/containers/common/tasks/docker_deploy.yml",
        "ansible/roles/containers/common/defaults/main.yml",
        "ansible/roles/containers/common/meta/main.yml",
    ],
)
def test_a_common_change_is_pi_work_not_broad_and_not_a_service(path):
    # common/ is the Pi's shared Compose deploy path, which daniel-box's play never reads. It
    # must not buy a full deploy.yml run there, and it is not a service named `common`.
    cs = services_from_changed_paths([path])
    assert cs.pi_shared is True
    assert cs.broad is False
    assert cs.services == cs.tasks == set()


def test_role_tasks_change_flags_tasks_not_deploy():
    # tasks/ isn't auto-deployed (structural — manual), but it must be FLAGGED so the deployer
    # defers-and-alerts instead of silently ff-merging: a tasks/ change alters what a deploy does,
    # so left unapplied with no signal it's the exact silent-drift the secrets/requirements paths
    # already close. It maps to cs.tasks (for the alert), NOT cs.services (no scoped redeploy).
    cs = services_from_changed_paths(
        ["ansible/roles/containers/prometheus/tasks/main.yml"]
    )
    assert cs.tasks == {"prometheus"}
    assert cs.services == set()
    assert cs.broad is False
    assert cs.secrets is False


def test_role_docs_do_not_trigger_deploy_or_flag():
    # A CLAUDE.md / doc edit is genuinely no-op (manual, as before) — not even flagged.
    cs = services_from_changed_paths(["ansible/roles/containers/prometheus/CLAUDE.md"])
    assert cs.services == set()
    assert cs.tasks == set()
    assert cs.broad is False


def test_template_and_tasks_same_service_deploys_and_flags_tasks():
    # A push that changes both a template and tasks/ for the same service deploys it (the scoped
    # --tags redeploy reruns the whole role incl. tasks), and also records the tasks flag.
    cs = services_from_changed_paths(
        [
            "ansible/roles/containers/prometheus/templates/prometheus.yml.j2",
            "ansible/roles/containers/prometheus/tasks/main.yml",
        ]
    )
    assert cs.services == {"prometheus"}
    assert cs.tasks == {"prometheus"}


# k8s roles (ansible/roles/k8s/<role>/...) matched NONE of the regexes above (all containers/-
# scoped), so services_from_changed_paths returned an EMPTY ChangeSet and main()'s `if not
# cs.services:` branch took it as a plain docs-only ff-merge — silent, on every has_gitops host
# (daniel-box, all 41 services platform: k8s). Maps to cs.k8s (defer-and-alert), never cs.services
# — this deployer has no mechanism that ever auto-deploys a k8s-platform role.
def test_k8s_role_change_flags_k8s_not_services():
    cs = services_from_changed_paths(
        ["ansible/roles/k8s/authelia/templates/deployment.yaml.j2"]
    )
    assert cs.k8s == {"authelia"}
    assert cs.services == set()
    assert cs.broad is False
    assert cs.tasks == set()


def test_k8s_role_matches_any_subdir_not_just_templates():
    # Unlike containers/ (split into templates/tasks/meta channels), a k8s role has no separate
    # auto-deploy path for any of its subdirs to scope against — the whole role dir shares one
    # channel.
    cs = services_from_changed_paths(["ansible/roles/k8s/authelia/tasks/main.yml"])
    assert cs.k8s == {"authelia"}
    assert cs.services == set()


def test_k8s_role_docs_stay_silent():
    cs = services_from_changed_paths(["ansible/roles/k8s/authelia/CLAUDE.md"])
    assert cs.k8s == set()
    assert cs.services == set()
    assert cs.broad is False


def test_k8s_and_docker_service_changes_are_independent():
    cs = services_from_changed_paths(
        [
            "ansible/roles/k8s/authelia/templates/deployment.yaml.j2",
            "ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2",
        ]
    )
    assert cs.k8s == {"authelia"}
    assert cs.services == {"cadvisor"}


# L2: a change under a container role's defaults/, vars/, or handlers/ matched none of the
# config/tasks/meta regexes and fell through to the silent docs-only ff-merge — a structural change
# that alters what a deploy does, applied with no signal. The role-root catch-all flags it on the
# tasks channel (defer-and-alert), so a future role adding one of these dirs can't regress silently.
def test_role_defaults_change_flags_tasks_not_deploy():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/prometheus/defaults/main.yml"]
    )
    assert cs.tasks == {"prometheus"}
    assert cs.services == set()
    assert cs.broad is False
    assert cs.secrets is False


def test_role_vars_and_handlers_change_flag_tasks():
    for sub in ("vars", "handlers"):
        cs = services_from_changed_paths(
            [f"ansible/roles/containers/sonarr/{sub}/main.yml"]
        )
        assert cs.tasks == {"sonarr"}, sub
        assert cs.services == set(), sub


def test_role_root_non_md_file_flags_tasks():
    # A non-doc file at the role root (not templates/files/tasks/meta) is structural too.
    cs = services_from_changed_paths(["ansible/roles/containers/prometheus/vars.yml"])
    assert cs.tasks == {"prometheus"}


def test_role_readme_md_stays_silent_like_claude_md():
    # *.md is a doc — the catch-all must not flag it (regression guard for the .md exclusion).
    cs = services_from_changed_paths(["ansible/roles/containers/prometheus/README.md"])
    assert cs.tasks == set()
    assert cs.services == set()
    assert cs.broad is False


def test_config_change_with_compose_change_dedupes_to_one_service():
    cs = services_from_changed_paths(
        [
            "ansible/roles/containers/traefik/templates/config.yml.j2",
            "ansible/roles/containers/traefik/templates/docker-compose.yml.j2",
        ]
    )
    assert cs.services == {"traefik"}


# ── role_of: the one answer to "which role directory is this path in" ──────────────
@pytest.mark.parametrize(
    "path,expected",
    [
        (
            "ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2",
            ("containers", "cadvisor", "templates"),
        ),
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
    ],
)
def test_role_of_is_none_for_a_path_in_no_role_directory(path):
    assert role_of(path) is None


def test_a_file_other_roles_ship_reaches_every_role_shipping_it():
    """deploy_ui and renovate_agent install the deployer's `gitops_markers.py` by path (#3306),
    so both re-apply beside its owner. Another deployer module reaches neither."""
    markers = ["ansible/roles/setup/gitops_deploy/files/gitops_markers.py"]
    roles = {"gitops_deploy", "deploy_ui", "renovate_agent"}
    assert services_from_changed_paths(markers).setup_roles == roles
    assert setup_tags_for(markers) == roles
    other = ["ansible/roles/setup/gitops_deploy/files/deploy_logic.py"]
    assert services_from_changed_paths(other).setup_roles == {"gitops_deploy"}
    assert setup_tags_for(other) == {"gitops_deploy"}


def test_a_host_lib_change_reaches_its_consumers_and_not_common():
    """`common` reaches a host only through the roles copying `host_lib.py` (#3312). The four
    initial_setup consumers apply on the tick, `k3s` is the one role left to record, and
    `common` is named nowhere. A `common` file no role ships still names `common`."""
    host_lib = ["ansible/roles/setup/common/files/host_lib.py"]
    consumers = {"fake_remux", "gitops_deploy", "renovate_agent", "renovate_notify"}
    assert services_from_changed_paths(host_lib).setup_roles == consumers | {"k3s"}
    assert setup_tags_for(host_lib) == consumers
    resolv = ["ansible/roles/setup/common/templates/resolv.conf.j2"]
    assert services_from_changed_paths(resolv).setup_roles == {"common"}


def test_a_common_task_file_change_reaches_the_roles_importing_it():
    """An `import_tasks` of a `common/tasks/` file is static, so the imported tasks run under
    each importer's tags and only re-applying the importers applies the change (#3317). The
    same four initial_setup roles apply on the tick, and `k3s` is the one left to record."""
    path = "ansible/roles/setup/common/tasks/install_host_lib.yml"
    consumers = {"fake_remux", "gitops_deploy", "renovate_agent", "renovate_notify"}
    assert setup_roles_for(path) == consumers | {"k3s"}
    assert setup_tags_for([path]) == consumers
