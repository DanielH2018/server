"""A test-suite path reaches no host, so it must drive no deploy decision.

Every prefix in deploy_changes matches on path alone, which made a test file read as whatever
plane it happened to sit under -- one under roles/setup/gitops_deploy/ parked the tick for
every session, one under roles/k8s/<svc>/files/ defer-alerted (PR #707, 2026-09-01). The
exemption has to be exact: a test beside a real change must not disarm the change, and a
path that merely contains `test` is not a test.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_changes_test_paths.py

from deploy_changes import services_from_changed_paths


# ── test-suite paths reach no host, so they drive no deploy decision ────────────────────
#
# Every prefix in this module matches on path alone, which made a test file read as whatever
# plane it happened to sit under. PR #707 changed three of them and hit two arms at once: a
# test under roles/setup/gitops_deploy/ set broad_manual and parked the tick for every session,
# and one under roles/k8s/<svc>/files/ set cs.k8s and defer-alerted. Clearing it took an
# operator running the playbook by hand and then an ff-merge (2026-09-01).
#
# The pairs below are clean/flagged per rule: a guard that classifies everything as a test and
# one that classifies nothing are indistinguishable from the passing side alone.

TEST_ONLY_PATHS = [
    # The repo-wide guards, plus the two support modules there that match no name pattern.
    "ansible/tests/k8s/test_k8s_manifests.py",
    "ansible/tests/_helpers.py",
    "ansible/tests/conftest.py",
    # A test beside the module it covers — the layout most roles/*/*/files/ suites use.
    "ansible/roles/k8s/qbittorrent/files/test_apply_prefs.py",
    "ansible/roles/k8s/monitor-bridge/files/conftest.py",
    # A role-local tests/ directory.
    "ansible/roles/k8s/home-assistant/tests/test_fan_macros.py",
    "ansible/roles/setup/gitops_deploy/tests/test_deploy_health.py",
]


def test_a_test_only_push_is_clean():
    """It must reach gitops_deploy.py's `if not cs.services` branch, which ff-merges."""
    cs = services_from_changed_paths(TEST_ONLY_PATHS)
    assert cs.broad is False
    assert cs.broad_manual is False
    assert cs.services == set()
    assert cs.k8s == set()
    assert cs.tasks == set()
    assert cs.secrets is False


def test_the_deployers_own_module_is_still_flagged():
    """The rejecting half. deploy_logic.py sits in the same directory as the test above and
    must still read as a setup-plane change the tick applies — not as the empty ChangeSet a
    test-only push produces, which would ff-merge it and leave the host on the old code."""
    cs = services_from_changed_paths(
        ["ansible/roles/setup/gitops_deploy/files/deploy_logic.py"]
    )
    assert cs.broad is True and cs.broad_setup is True
    assert cs.setup_roles == {"gitops_deploy"}


def test_a_test_file_does_not_disarm_a_real_change_beside_it():
    """The case an over-broad predicate breaks. The plane flags are ORed across the push, so
    one exempt path must not clear the flag a sibling set — a half-applied broad change is
    exactly what the arm exists to prevent."""
    cs = services_from_changed_paths(
        [
            "ansible/roles/setup/gitops_deploy/tests/test_deploy_health.py",
            "ansible/roles/setup/gitops_deploy/files/deploy_logic.py",
        ]
    )
    assert cs.broad_setup is True
    assert cs.setup_roles == {"gitops_deploy"}


def test_a_k8s_service_change_is_still_flagged_beside_its_test():
    """The same rejecting half one plane over: the test is exempt, the manifest is not."""
    cs = services_from_changed_paths(
        [
            "ansible/roles/k8s/qbittorrent/files/test_apply_prefs.py",
            "ansible/roles/k8s/qbittorrent/templates/deployment.yaml.j2",
        ]
    )
    assert "qbittorrent" in cs.k8s


def test_a_path_merely_containing_test_is_not_exempt():
    """`latest`/`contest` end in `test`; a directory named `tests` is not a path segment
    called `testdata`. Substring matching here would exempt real templates."""
    for path in (
        "ansible/roles/k8s/sonarr/templates/latest.yaml.j2",
        "ansible/roles/k8s/sonarr/testsuite-config/values.yml.j2",
        "ansible/roles/setup/gitops_deploy/files/pytest_helper.py",
    ):
        cs = services_from_changed_paths([path])
        assert cs.broad or cs.services or cs.k8s or cs.tasks or cs.meta, path
