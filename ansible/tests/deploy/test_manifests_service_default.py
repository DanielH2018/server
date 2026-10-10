"""`manifests_service` defaults to the calling role's name, also inside the nested includes.

No caller passes `manifests_service` since #4053, so the default in
`ansible/roles/k8s/manifests/defaults/main.yml` names every service's manifest directory, release
record and `homelab/role` label. Ansible templates a role default where it is read. Inside the
volume-snapshot and volume-revert includes that `k8s/manifests` makes, the nearest parent role
is `k8s/manifests` itself, so the default has to skip it to reach the caller.

Run: uv run pytest ansible/tests/deploy/test_manifests_service_default.py
"""

from _helpers import ANSIBLE, load_yaml, render_expr

DEFAULTS = ANSIBLE / "roles/k8s/manifests/defaults/main.yml"

# What `ansible_parent_role_names` holds at each read: nearest parent first.
IN_THE_INCLUDE = ["k8s/sonarr"]
IN_A_NESTED_INCLUDE = ["k8s/manifests", "k8s/sonarr"]


def _service(expression: str, parents: list[str]) -> str:
    return render_expr(expression, ansible_parent_role_names=parents)


def test_the_default_names_the_caller_at_both_depths_is_clean():
    expression = load_yaml(DEFAULTS)["manifests_service"]
    assert _service(expression, IN_THE_INCLUDE) == "sonarr"
    assert _service(expression, IN_A_NESTED_INCLUDE) == "sonarr"


def test_a_default_that_takes_the_nearest_parent_is_flagged():
    # The shape without the `reject`, measured in a scratch play on 2026-10-10: the nested
    # include read `manifests` and would have snapshotted under the wrong service.
    naive = "{{ ansible_parent_role_names | first | basename }}"
    assert _service(naive, IN_A_NESTED_INCLUDE) != "sonarr"
