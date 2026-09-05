"""Guard: every scrape job that targets daniel-pi carries a `scrape_timeout` above the default.

WHY. The Pi is the one host outside the cluster, and it is contended: load5 of 2.4-6.6 on four
slow cores, CPU PSI around 30% `some`. A scrape that lands in a contended stretch takes seconds
rather than its 0.2-0.6s median, and one that crosses Prometheus' 10s default reads as
`up == 0`. Over the 7 days to 2026-09-03 that was 88 minutes on node-pi and 36 on alloy-pi,
each a single-cycle DOWN on the Cluster Scrape Targets monitor with a healthy exporter behind
it (#930). The per-job `scrape_timeout: 30s` is what stops the flap, and a job added for the Pi
later (a third exporter, a cAdvisor) inherits the flap unless it carries the same override.

WHY PER JOB. The override is deliberately NOT global: an in-cluster target that takes 30s is
the failure the monitor exists to surface, so those jobs keep the 10s default. This test holds
both halves — the Pi jobs above the default, every other job at it.
"""

import re
import sys as _sys

import pytest
from _helpers import ANSIBLE as _ANSIBLE
from _helpers import REPO as _REPO

_sys.path.insert(0, str(_ANSIBLE / "tests"))
_sys.path.insert(0, str(_REPO / "scripts" / "validate"))

from lib import yaml_fast

from _k8s_render import rendered_docs

_GROUP_VARS = _ANSIBLE / "inventory/group_vars/all.yml"

# Prometheus' own default when a job sets none.
_DEFAULT_TIMEOUT_S = 10

# The jobs the census must find. A Pi job renamed or moved out of the static block would
# otherwise leave `_pi_jobs()` empty and the loop below vacuously green.
KNOWN_PI_JOBS = frozenset({"node-pi", "alloy-pi"})

_DURATION = re.compile(r"^(\d+)(ms|s|m|h)$")
_UNIT_S = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}


def _seconds(duration: str) -> float:
    match = _DURATION.match(duration)
    assert match, f"unparseable Prometheus duration {duration!r}"
    return int(match.group(1)) * _UNIT_S[match.group(2)]


def timeout_problem(job: dict, *, on_pi: bool) -> str | None:
    """The failure message for one scrape job, else None.

    A Pi job must set a timeout above the 10s default and no longer than its interval
    (Prometheus refuses the config otherwise). A cluster job must set none at all.
    """
    name = job["job_name"]
    timeout = job.get("scrape_timeout")
    if not on_pi:
        if timeout is None:
            return None
        return f"{name} overrides scrape_timeout ({timeout}); only the Pi jobs may"
    if timeout is None:
        return f"{name} targets daniel-pi but has no scrape_timeout, so it flaps at 10s"
    if _seconds(timeout) <= _DEFAULT_TIMEOUT_S:
        return f"{name} sets scrape_timeout {timeout}, no longer than the 10s default"
    interval = job.get("scrape_interval", "1m")
    if _seconds(timeout) > _seconds(interval):
        return f"{name} sets scrape_timeout {timeout} above its {interval} interval"
    return None


def _scrape_jobs() -> list[dict]:
    """Every scrape job out of the rendered prometheus ConfigMap's embedded prometheus.yml."""
    for role, template, doc in rendered_docs():
        if role != "claude-otel" or "prometheus" not in str(template):
            continue
        if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
            continue
        return yaml_fast.safe_load(doc["data"]["prometheus.yml"])["scrape_configs"]
    pytest.fail("no prometheus ConfigMap rendered — this guard is watching nothing")


def _targets_pi(job: dict, pi_ip: str) -> bool:
    return any(
        target.startswith(f"{pi_ip}:")
        for block in job.get("static_configs") or []
        for target in block.get("targets") or []
    )


def test_every_rendered_job_has_the_right_timeout() -> None:
    pi_ip = yaml_fast.safe_load(_GROUP_VARS.read_text())["k8s_pi_client_ip"]
    jobs = _scrape_jobs()
    pi_jobs = {job["job_name"] for job in jobs if _targets_pi(job, pi_ip)}
    missing = KNOWN_PI_JOBS - pi_jobs
    assert not missing, (
        f"census lost the Pi jobs {sorted(missing)}; found {sorted(pi_jobs)}"
    )
    problems = [
        problem
        for job in jobs
        if (problem := timeout_problem(job, on_pi=job["job_name"] in pi_jobs))
    ]
    assert not problems, "\n".join(problems)


def test_a_pi_job_with_a_30s_timeout_is_clean() -> None:
    job = {"job_name": "node-pi", "scrape_interval": "1m", "scrape_timeout": "30s"}
    assert timeout_problem(job, on_pi=True) is None


def test_a_pi_job_without_a_timeout_is_flagged() -> None:
    """The shape a third Pi exporter lands in when copied from a cluster job."""
    job = {"job_name": "cadvisor-pi", "scrape_interval": "1m"}
    assert timeout_problem(job, on_pi=True) == (
        "cadvisor-pi targets daniel-pi but has no scrape_timeout, so it flaps at 10s"
    )


def test_a_pi_job_at_the_default_is_flagged() -> None:
    job = {"job_name": "node-pi", "scrape_interval": "1m", "scrape_timeout": "10s"}
    assert timeout_problem(job, on_pi=True) == (
        "node-pi sets scrape_timeout 10s, no longer than the 10s default"
    )


def test_a_pi_job_above_its_interval_is_flagged() -> None:
    job = {"job_name": "node-pi", "scrape_interval": "1m", "scrape_timeout": "2m"}
    assert timeout_problem(job, on_pi=True) == (
        "node-pi sets scrape_timeout 2m above its 1m interval"
    )


def test_a_cluster_job_with_no_timeout_is_clean() -> None:
    assert (
        timeout_problem({"job_name": "longhorn", "scrape_interval": "1m"}, on_pi=False)
        is None
    )


def test_a_cluster_job_that_overrides_the_timeout_is_flagged() -> None:
    job = {"job_name": "longhorn", "scrape_interval": "1m", "scrape_timeout": "30s"}
    assert timeout_problem(job, on_pi=False) == (
        "longhorn overrides scrape_timeout (30s); only the Pi jobs may"
    )
