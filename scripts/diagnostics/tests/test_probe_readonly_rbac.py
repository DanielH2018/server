"""Tests for `probe.py readonly-rbac`.

The probe asserts that plain kubectl is still the read-only ServiceAccount. That guarantee is
what every agent session leans on — Ansible is meant to be the only write path to this cluster —
and nothing else notices if the RBAC widens.

The load-bearing case is `test_a_refused_control_is_inconclusive_not_a_pass`. A probe that only
checks DENIALS goes green when kubectl is broken outright, because a missing kubeconfig refuses
everything. That is the `an-optimisation-can-land-green-and-be-inert` shape: passing for the
wrong reason, indistinguishable from passing for the right one.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import probe_readonly_rbac as ph

_CONTROLS_OK = {("get", "pods"): True, ("list", "deployments"): True}
_CONTROLS_REFUSED = {("get", "pods"): False, ("list", "deployments"): True}
_ALL_FORBIDDEN = {("get", "secrets"): False, ("create", "pods"): False}


def test_a_read_only_service_account_passes():
    text, code = ph.format_readonly_rbac(_ALL_FORBIDDEN, _CONTROLS_OK)
    assert code == 0 and "OK:" in text


def test_privilege_creep_fails_and_names_the_verb():
    denied = {("get", "secrets"): True, ("create", "pods"): False}
    text, code = ph.format_readonly_rbac(denied, _CONTROLS_OK)
    assert code == 1
    assert "FAIL" in text and "get secrets" in text
    assert "create pods" not in text.split("FAIL")[1], (
        "only the permitted verb is the finding"
    )


def test_a_refused_control_is_inconclusive_not_a_pass():
    """The whole reason the controls exist.

    Every denial reads `forbidden` here, which on its own is the healthy answer — but the
    control is refused, so the tool cannot talk to the cluster and the denials prove nothing.
    Exit 2, distinct from both the pass and the creep.
    """
    text, code = ph.format_readonly_rbac(_ALL_FORBIDDEN, _CONTROLS_REFUSED)
    assert code == 2
    assert "INCONCLUSIVE" in text
    assert "get pods" in text


def test_an_inconclusive_run_outranks_creep():
    """If the tool cannot be trusted, a `PERMITTED` reading cannot be trusted either."""
    denied = {("get", "secrets"): True}
    _text, code = ph.format_readonly_rbac(denied, _CONTROLS_REFUSED)
    assert code == 2


def test_the_denied_set_covers_secrets_and_pod_writes():
    """A shrinking denial list would quietly stop checking. Pin what must be in it."""
    assert ("get", "secrets") in ph.READONLY_DENIED
    assert ("list", "secrets") in ph.READONLY_DENIED
    assert ("create", "pods") in ph.READONLY_DENIED
    assert ("delete", "pods") in ph.READONLY_DENIED


def test_the_controls_are_disjoint_from_the_denials():
    """A verb in both lists would make the probe assert two contradictory things."""
    assert not set(ph.READONLY_ALLOWED) & set(ph.READONLY_DENIED)


def test_can_i_asks_rbac_rather_than_attempting_the_write():
    """`create pods` must never be probed by creating a pod on a read-only cluster."""
    argv = ph.can_i_argv("create", "pods", "homelab")
    assert argv[:3] == ["kubectl", "auth", "can-i"]
    assert "--quiet" in argv and "-n" in argv
