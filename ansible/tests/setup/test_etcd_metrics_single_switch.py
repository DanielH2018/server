"""Pins the etcd-metrics port and its only consumer to ONE variable.

Two roles have to agree for etcd metrics to be readable, and they are applied by different
playbooks: `setup/k3s` puts `--etcd-expose-metrics` in the server args (k3s-bringup.yml, a
broad-plane run), and `observability` gates its `etcd` scrape job (deploy.yml). Either half
alone is a silent fault rather than a loud one:

  * flag without job  — the port binds 0.0.0.0 for nobody. A control-plane port is open, and
    the reason it was opened is nowhere in the running config.
  * job without flag  — Prometheus scrapes a loopback-bound port from the pod network, the
    target reads down forever, and Scrape Targets pages for a service that was never armed.

Neither shows up in a single render, because both halves render perfectly well on their own.
So the guard is that both halves move with `k3s_etcd_expose_metrics`, defined once in
group_vars/all.yml, exactly as `k3s_audit_log_path` is shared by setup/k3s and loki-homelab's
Alloy shipper for the same two-roles-one-fact reason.

The scrape half is read from TWO renders of `prometheus.yaml.j2`, one with the switch armed and
one with it off, rather than from the template's text. Reading the text meant walking back to
the nearest `{% if %}` line and matching the variable's name in it — which holds only while the
gate is spelled that way, and a condition spelled any other way left the guard matching nothing
(#3202). The two renders say the same thing about the live template and also fail on a gate
keyed to a different variable.

This deliberately does not assert the variable's VALUE. Off is the shipped default and armed
is a legitimate operator choice; what must not happen is the two halves diverging.
"""

import re

from _helpers import REPO
from _k8s_render import render_role_template

_REPO = REPO
_ALL_VARS = _REPO / "ansible/inventory/group_vars/all.yml"
_K3S_DEFAULTS = _REPO / "ansible/roles/setup/k3s/defaults/main.yml"

_SWITCH = "k3s_etcd_expose_metrics"
_JOB = "- job_name: etcd"


def _prometheus_config(switch: bool) -> str:
    """The rendered Prometheus config with the etcd switch set to `switch`."""
    return render_role_template(
        "observability", "prometheus.yaml.j2", {_SWITCH: switch}
    )


def test_the_switch_is_defined_once_in_group_vars():
    """The shared fact lives in all.yml, not in either role's defaults.

    A role-local default would let the other role fall through to its own `| default(false)`
    and read the opposite value, which is precisely the drift this file exists to stop.
    """
    assert re.search(rf"^{_SWITCH}:", _ALL_VARS.read_text(), re.M), (
        f"{_SWITCH} must be defined in group_vars/all.yml — two roles read it"
    )

    for role_defaults in (_K3S_DEFAULTS,):
        assert not re.search(rf"^{_SWITCH}:", role_defaults.read_text(), re.M), (
            f"{_SWITCH} must NOT be redefined in {role_defaults.relative_to(_REPO)}; "
            "a role-local default shadows the shared one and lets the halves drift"
        )


def test_the_k3s_flag_is_gated_on_the_switch():
    """--etcd-expose-metrics appears only inside a conditional on the switch."""
    text = _K3S_DEFAULTS.read_text()
    flag_lines = [ln for ln in text.splitlines() if "--etcd-expose-metrics" in ln]

    assert flag_lines, "setup/k3s must offer --etcd-expose-metrics in k3s_server_args"
    for line in flag_lines:
        assert _SWITCH in line, (
            "--etcd-expose-metrics must be gated on "
            f"{_SWITCH}, got an ungated: {line.strip()}"
        )


def test_the_scrape_job_is_gated_on_the_same_switch():
    """The `etcd` job appears when the switch is armed and not when it is off.

    Both directions, because each alone passes on a defect: present-when-armed passes on an
    unconditional job, and absent-when-off passes on a job that never renders at all.
    """
    armed = _prometheus_config(True)
    assert _JOB in armed, (
        f"observability declares no `etcd` scrape job even with {_SWITCH} armed, so arming "
        "the switch opens a control-plane port for nobody"
    )
    off = _prometheus_config(False)
    assert _JOB not in off, (
        f"the `etcd` job renders with {_SWITCH} off, so Prometheus scrapes a loopback-bound "
        "port from the pod network and Scrape Targets pages for a service never armed"
    )


def test_the_scrape_target_is_the_metrics_port_not_the_client_port():
    """2381 (metrics, plain HTTP) — never 2379, which is the client port and needs certs.

    Pointing the job at 2379 is the plausible wrong answer: it is the port etcd is normally
    associated with, it IS listening, and the scrape fails with a TLS error rather than a
    connection refused — which reads as a cert problem to fix rather than a wrong target.
    """
    lines = _prometheus_config(True).splitlines()
    job_line = next(i for i, ln in enumerate(lines) if ln.strip() == _JOB)
    block = "\n".join(lines[job_line : job_line + 8])

    assert ":2381'" in block, "the etcd job must scrape the 2381 metrics port"
    assert ":2379" not in block, (
        "2379 is etcd's CLIENT port — it requires client certs Prometheus does not carry"
    )
