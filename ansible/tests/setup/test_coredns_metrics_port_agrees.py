"""Guard: the port daniel-box's host CoreDNS publishes must equal the one Prometheus scrapes.

WHY THE VALUE IS WRITTEN TWICE. `k3s_host_dns_metrics_port` is a default of `roles/setup/k3s`,
and a role's defaults are in scope only while that role runs. The k8s play never includes it,
so a manifest referencing it across the plane boundary reads as undefined rather than shared —
which is why `k8s_node_dns_metrics_port` exists in group_vars as a second copy.

WHY THAT NEEDS A GUARD. The two live in different planes and are applied by different
playbooks (`k3s-bringup.yml` and `deploy.yml`), so nothing makes them move together. Editing
one is a one-line change that passes lint, renders valid YAML, and deploys green on its own
plane while leaving the scrape job pointed at a closed port.

The failure is loud rather than silent — `check_cluster_targets` fails on any member at 0, so
the **Scrape Targets** monitor goes red — but loud-at-3am is a poor substitute for caught-in-CI,
and the monitor cannot say WHY the target is down. This test can.
"""

from __future__ import annotations

import yaml
from _helpers import ROLES as _ROLES

_GROUP_VARS = _ROLES.parent / "inventory/group_vars/all.yml"
_K3S_DEFAULTS = _ROLES / "setup/k3s/defaults/main.yml"

_SCRAPER_VAR = "k8s_node_dns_metrics_port"
_PUBLISHER_VAR = "k3s_host_dns_metrics_port"


def ports_disagree(publisher: int, scraper: int) -> str | None:
    """The failure message when the two copies differ, else None."""
    if publisher == scraper:
        return None
    return (
        f"{_PUBLISHER_VAR} is {publisher} but {_SCRAPER_VAR} is {scraper}; the coredns-host "
        "scrape job would target a closed port"
    )


def test_the_live_ports_agree() -> None:
    publisher = yaml.safe_load(_K3S_DEFAULTS.read_text())[_PUBLISHER_VAR]
    scraper = yaml.safe_load(_GROUP_VARS.read_text())[_SCRAPER_VAR]
    problem = ports_disagree(publisher, scraper)
    assert problem is None, problem


def test_matching_ports_are_clean() -> None:
    assert ports_disagree(9253, 9253) is None


def test_mismatched_ports_are_flagged() -> None:
    """The one-line edit on either plane that leaves the job scraping nothing."""
    assert ports_disagree(9253, 9254) == (
        "k3s_host_dns_metrics_port is 9253 but k8s_node_dns_metrics_port is 9254; the "
        "coredns-host scrape job would target a closed port"
    )


def test_the_scrape_job_reads_the_group_var_rather_than_a_literal() -> None:
    """A literal port in the template would pass the comparison above while ignoring it."""
    template = (_ROLES / "k8s/claude-otel/templates/prometheus.yaml.j2").read_text()
    assert "{{ k8s_node_client_ip }}:{{ k8s_node_dns_metrics_port }}" in template, (
        "the coredns-host job no longer interpolates both group vars; this guard would "
        "compare two values the rendered config does not use"
    )
