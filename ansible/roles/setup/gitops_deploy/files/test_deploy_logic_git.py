"""What the tick decides from git state alone: deploy, skip, or do nothing.

`is_diverged` is the load-bearing one — local and origin each holding commits the other lacks
means an automated pull would either lose work or conflict, so the tick has to stop and say so.
A dirty tree outranks every other reason to deploy.
"""

# ansible/roles/setup/gitops_deploy/files/test_deploy_logic.py

from deploy_logic import (
    next_action,
    is_diverged,
    container_names,
    containers_to_gate,
)


def test_next_action_noop_when_in_sync():
    assert next_action("aaa", "aaa", None) == "noop"


def test_next_action_skip_when_origin_is_hold():
    assert next_action("aaa", "bad", "bad") == "skip_hold"


def test_next_action_deploy_when_origin_ahead():
    assert next_action("aaa", "bbb", None) == "deploy"


def test_next_action_deploy_when_hold_is_stale():
    # origin advanced past the held bad SHA (operator reverted) -> deploy again
    assert next_action("aaa", "ccc", "bad") == "deploy"


def test_next_action_dirty_tree_skips_even_in_sync():
    # A dirty working tree is a *healthy* skip (operator mid-edit), not an outage.
    # It must short-circuit to "dirty" so main() can still push liveness instead
    # of going silent and falsely tripping the push monitor's dead-man's-switch.
    assert next_action("aaa", "aaa", None, dirty=True) == "dirty"


def test_next_action_dirty_tree_never_deploys():
    # Must NOT deploy from a dirty tree even when origin has advanced — dirty
    # takes precedence over every other outcome.
    assert next_action("aaa", "bbb", None, dirty=True) == "dirty"


def test_next_action_clean_tree_still_deploys():
    # Regression: a clean tree (the default) behaves exactly as before.
    assert next_action("aaa", "bbb", None, dirty=False) == "deploy"


# The deployer is pull-based and only ever fast-forwards: it must act ONLY when
# origin is strictly ahead of local. When the operator has committed locally but
# not pushed, origin is an *ancestor* of local (origin_ahead=False). The old code
# saw origin != local and returned "deploy", then diffed local..origin (the reverse
# of the un-pushed commits) and mis-fired a deploy + false rollback. Must be a no-op.
def test_next_action_noop_when_local_ahead_of_origin():
    assert next_action("localnew", "originold", None, origin_ahead=False) == "noop"


def test_next_action_deploy_requires_origin_ahead():
    # The normal pull path: origin strictly ahead (the default) still deploys.
    assert next_action("aaa", "bbb", None, origin_ahead=True) == "deploy"


def test_next_action_dirty_precedes_origin_ahead_check():
    # dirty still short-circuits even when origin isn't ahead.
    assert (
        next_action("localnew", "originold", None, dirty=True, origin_ahead=False)
        == "dirty"
    )


# is_diverged: local↔origin diverged (neither an ancestor of the other) → the deployer noops
# forever while origin's new commits never deploy; surfaced via GitOps Status (review L3).
def test_is_diverged_true_when_neither_is_ancestor():
    assert is_diverged("originX", "localY", origin_ahead=False, local_ahead=False)


def test_is_diverged_false_when_origin_ahead():
    # normal pull path — fast-forwardable, deploys.
    assert not is_diverged("originX", "localY", origin_ahead=True, local_ahead=False)


def test_is_diverged_false_when_local_ahead_unpushed():
    # committed-but-unpushed local commit is a plain noop (secret-rotate's domain), not divergence.
    assert not is_diverged("originX", "localY", origin_ahead=False, local_ahead=True)


def test_is_diverged_false_when_in_sync():
    assert not is_diverged("same", "same", origin_ahead=True, local_ahead=True)


# The health gate must only check services actually deployed on THIS host. A
# changed template for an other-host-only service (dozzle is daniel-pi-only)
# renders no compose here, so containers_for() reads no file and passes None.
# Gating it would poll a phantom container until timeout and false-rollback.
def test_containers_to_gate_skips_service_not_on_this_host():
    assert containers_to_gate(None, "dozzle") == []


def test_containers_to_gate_uses_rendered_container_names():
    compose = "    container_name: scrutiny-influxdb\n    container_name: scrutiny\n"
    assert containers_to_gate(compose, "scrutiny") == ["scrutiny-influxdb", "scrutiny"]


def test_containers_to_gate_falls_back_to_service_when_compose_names_none():
    # Present compose that declares no container_name -> gate the role/service name.
    assert containers_to_gate("    image: foo\n", "freshrss") == ["freshrss"]


# A role may run several containers; the bumped image's container is often NOT
# the role-named one (e.g. cadvisor lives in the prometheus role). The health
# gate must inspect the actual container_name values from the rendered compose.
def test_container_names_multi_container():
    compose = (
        "services:\n"
        "  influxdb:\n"
        "    container_name: scrutiny-influxdb\n"
        "  web:\n"
        "    container_name: scrutiny\n"
        "  collector:\n"
        "    container_name: scrutiny-collector\n"
    )
    assert container_names(compose) == [
        "scrutiny-influxdb",
        "scrutiny",
        "scrutiny-collector",
    ]


def test_container_names_strips_quotes():
    assert container_names('    container_name: "cadvisor"\n') == ["cadvisor"]


def test_container_names_ignores_other_keys():
    compose = (
        "    image: ghcr.io/google/cadvisor:v0.53.0\n    restart: unless-stopped\n"
    )
    assert container_names(compose) == []


def test_container_names_dedupes():
    assert container_names("    container_name: a\n    container_name: a\n") == ["a"]


def test_container_names_empty():
    assert container_names("") == []
