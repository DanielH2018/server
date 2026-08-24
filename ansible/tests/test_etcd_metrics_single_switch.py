"""Pins the etcd-metrics port and its only consumer to ONE variable.

Two roles have to agree for etcd metrics to be readable, and they are applied by different
playbooks: `setup/k3s` puts `--etcd-expose-metrics` in the server args (k3s-bringup.yml, a
broad-plane run), and `claude-otel` gates its `etcd` scrape job (deploy.yml). Either half
alone is a silent fault rather than a loud one:

  * flag without job  — the port binds 0.0.0.0 for nobody. A control-plane port is open, and
    the reason it was opened is nowhere in the running config.
  * job without flag  — Prometheus scrapes a loopback-bound port from the pod network, the
    target reads down forever, and Scrape Targets pages for a service that was never armed.

Neither shows up in a render check, because both halves render perfectly well on their own.
So the guard is that both read the SAME variable name — `k3s_etcd_expose_metrics`, defined
once in group_vars/all.yml, exactly as `k3s_audit_log_path` is shared by setup/k3s and
loki-homelab's promtail for the same two-roles-one-fact reason.

This deliberately does not assert the variable's VALUE. Off is the shipped default and armed
is a legitimate operator choice; what must not happen is the two halves diverging.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ALL_VARS = _REPO / "ansible/inventory/group_vars/all.yml"
_K3S_DEFAULTS = _REPO / "ansible/roles/setup/k3s/defaults/main.yml"
_PROM_TEMPLATE = _REPO / "ansible/roles/k8s/claude-otel/templates/prometheus.yaml.j2"

_SWITCH = "k3s_etcd_expose_metrics"


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
    """The `etcd` job sits inside an `{% if %}` on the switch, not a different one."""
    lines = _PROM_TEMPLATE.read_text().splitlines()

    job_line = next(
        (i for i, ln in enumerate(lines) if ln.strip() == "- job_name: etcd"), None
    )
    assert job_line is not None, "claude-otel must declare an `etcd` scrape job"

    # Walk back to the nearest enclosing Jinja conditional and require it to be ours.
    guard = next(
        (
            lines[i]
            for i in range(job_line, -1, -1)
            if lines[i].lstrip().startswith("{% if ")
        ),
        None,
    )
    assert guard is not None, (
        "the `etcd` scrape job must be conditional, not unconditional"
    )
    assert _SWITCH in guard, (
        f"the `etcd` job must be gated on {_SWITCH} so it cannot arm without the "
        f"port being opened; found: {guard.strip()}"
    )


def test_the_scrape_target_is_the_metrics_port_not_the_client_port():
    """2381 (metrics, plain HTTP) — never 2379, which is the client port and needs certs.

    Pointing the job at 2379 is the plausible wrong answer: it is the port etcd is normally
    associated with, it IS listening, and the scrape fails with a TLS error rather than a
    connection refused — which reads as a cert problem to fix rather than a wrong target.
    """
    lines = _PROM_TEMPLATE.read_text().splitlines()
    job_line = next(i for i, ln in enumerate(lines) if ln.strip() == "- job_name: etcd")
    block = "\n".join(lines[job_line : job_line + 8])

    assert ":2381'" in block, "the etcd job must scrape the 2381 metrics port"
    assert ":2379" not in block, (
        "2379 is etcd's CLIENT port — it requires client certs Prometheus does not carry"
    )
