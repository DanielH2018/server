"""A `secrets.yml` range with no deploy-plane path applies the k8s roles reading its keys (#5577).

`narrow` maps a moved key to its k8s readers (#5409), but `handle_broad` asks it only for a
range that carries a deploy-plane path. A rotation alone took the no-services branch: merged,
paged, and the reader kept the old value until a hand redeployed it.

`tick.secret_k8s` is the scripted `secret_setup_readers.py --deploy-tags`, an `(exit code,
stdout)` pair with `deploy_tags.py narrow`'s contract. Its default `(0, "")` is a range whose
keys no k8s role reads.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_secret_k8s_readers.py
"""

import subprocess

from _deploy_fakes import receipt_applied
from gitops_ledger import OWED_K8S_UNAPPLIED

ORIGIN = "2" * 40
SECRETS = "ansible/vars/secrets.yml"
HOST_VARS = "ansible/inventory/host_vars/test-host.yml"
GROUP_VARS = "ansible/inventory/group_vars/all.yml"
CLAUDE_TRIAGE_TASKS = "ansible/roles/setup/claude_code/tasks/triage.yml"
DECLARES = (
    "containers_list:\n  - name: homepage\n    platform: k8s\n"
    "  - name: peanut\n    platform: k8s\n"
)


def _owed(state) -> list[str]:
    return sorted(e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED))


def test_a_rotation_applies_the_k8s_roles_reading_its_key(gitops_deploy, tick, state):
    """b07b33061's shape: `peanut_password` rotated, read by homepage and peanut.

    The page still goes out: it names consumers no derivation sees, such as a Pi service.
    """
    tick.declare(DECLARES)
    tick.paths = [SECRETS]
    tick.secret_k8s = (0, "homepage,peanut")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks[0][-3:] == ["ansible/deploy.yml", "--tags", "homepage,peanut"]
    assert len(tick.playbooks) == 1
    assert tick.merges == [ORIGIN]
    assert receipt_applied(state) == {"ansible/deploy.yml": ("homepage", "peanut")}
    assert _owed(state) == []
    assert any("`secrets.yml` changed" in post for post in tick.posts)


def test_a_rotation_no_k8s_role_reads_applies_nothing(gitops_deploy, tick, state):
    """The lookup ran and answered nothing, so the range keeps the no-services branch."""
    tick.declare(DECLARES)
    tick.paths = [SECRETS]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert ("secret_k8s", [tick.local, ORIGIN]) in tick.log
    assert tick.playbooks == []
    assert tick.merges == [ORIGIN]
    assert receipt_applied(state) is None


def test_a_flux_owned_reader_is_left_to_flux(gitops_deploy, tick, state):
    """Flux renders a flux-owned role's secret from the cluster-vars file, not this play."""
    tick.declare(DECLARES)
    tick.files[f"{ORIGIN}:{HOST_VARS}"] = DECLARES.replace(
        "name: peanut\n    platform: k8s\n",
        "name: peanut\n    platform: k8s\n    owner: flux\n",
    )
    tick.paths = [SECRETS]
    tick.secret_k8s = (0, "homepage,peanut")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks[0][-1] == "homepage"


def test_a_refusal_naming_its_readers_owes_them(gitops_deploy, tick, state):
    """The fleet-coverage ceiling: too many readers to apply, each named in `k8s_unapplied`."""
    tick.declare(DECLARES)
    tick.paths = [SECRETS]
    tick.secret_k8s = (3, "homepage,peanut")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    assert tick.merges == [ORIGIN]
    assert _owed(state) == ["homepage", "peanut"]


def _merged_and_paged_owing_nothing(gitops_deploy, tick, state) -> None:
    """One tick over a rotation whose k8s lookup did not answer with tags.

    The caller asserts the journal line naming its arm, so the lookup is known to have run.
    """
    tick.declare(DECLARES)
    tick.paths = [SECRETS]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    assert tick.merges == [ORIGIN]
    assert _owed(state) == []
    assert any("`secrets.yml` changed" in post for post in tick.posts)


def test_a_nameless_refusal_merges_and_pages_without_owing_the_fleet(
    gitops_deploy, tick, capsys, state
):
    """#5409's regression in reverse: a refusal owing every declared service, for one key."""
    tick.secret_k8s = (3, "")
    _merged_and_paged_owing_nothing(gitops_deploy, tick, state)
    assert "no k8s reader routed (exit 3)" in capsys.readouterr().out


def test_a_failed_lookup_merges_and_pages_rather_than_parking(
    gitops_deploy, tick, capsys, state
):
    """A park on a failure that repeats, such as a checkout whose script predates
    `--deploy-tags`, would hold every landing behind one rotation."""
    tick.secret_k8s_error = subprocess.TimeoutExpired(["uv"], 30)
    _merged_and_paged_owing_nothing(gitops_deploy, tick, state)
    assert "k8s lookup failed (TimeoutExpired" in capsys.readouterr().out


def test_a_range_with_a_deploy_plane_path_leaves_its_keys_to_narrow(
    gitops_deploy, tick, state
):
    """`narrow` maps the keys of such a range itself, so the lookup would apply them twice."""
    tick.declare(DECLARES)
    tick.paths = [SECRETS, GROUP_VARS]
    tick.narrow = (0, "peanut")
    tick.secret_k8s = (0, "homepage,peanut")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert [argv[-1] for argv in tick.playbooks] == ["peanut"]
    assert not any(entry[0] == "secret_k8s" for entry in tick.log)


def test_a_key_both_planes_read_applies_the_setup_role_first(
    gitops_deploy, tick, state
):
    tick.declare(DECLARES)
    tick.paths = [SECRETS]
    tick.secret_readers = [CLAUDE_TRIAGE_TASKS]
    tick.secret_k8s = (0, "homepage")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert [argv[-3:] for argv in tick.playbooks] == [
        ["ansible/initial_setup.yml", "--tags", "claude_code"],
        ["ansible/deploy.yml", "--tags", "homepage"],
    ]
