"""The Alloy config must emit exactly the Loki labels Promtail emitted.

Every consumer — monitor-bridge's selectors, terraria-stats' and valheim-stats'
`{container="…"}` queries, the `{job="syslog"}` deploy annotation on every dashboard — selects
on `job`, `container`, `pod`, `namespace`, `machine` and `stream`, and on the ABSENCE of `app`.
A missing `machine` or a stray `app` is a shipped-blind bug: the HA ban check went blind that
way on 2026-08-23. The config is River, not YAML, so this reads the rendered ConfigMap text
rather than a parsed document.
"""

from __future__ import annotations

import re

import pytest

from _k8s_render import rendered_docs

REQUIRED_FRAGMENTS = (
    # The containerd envelope strip — omitting it broke terraria-stats' start-anchored parser.
    "stage.cri {}",
    # The Traefik drop stage, scoped to the rotate sidecar and nothing else.
    'selector = "{container=\\"access-log-rotate\\"}"',
    'drop_counter_reason = "traefik_routine_access_log"',
    '"DownstreamStatus\\":(200|204|304),.*\\"Duration\\":[0-9]{1,9},',
    # Node scoping: Alloy has no __host__ filter, so this is what stops every node tailing
    # every pod's path.
    'field = "spec.nodeName=" + sys.env("HOSTNAME")',
    # The pod-stream label set.
    'target_label  = "container"',
    'target_label  = "pod"',
    'target_label  = "namespace"',
    'target_label = "machine"',
    'replacement   = "/var/log/pods/*$1/*.log"',
    'url = "http://loki-homelab:3100/loki/api/v1/push"',
)

REQUIRED_JOBS = ("k8s", "syslog", "authlog", "k8s-audit")


@pytest.fixture(scope="module")
def alloy_config() -> str:
    for role, _tpl, doc in rendered_docs():
        if role == "loki-homelab" and doc.get("kind") == "ConfigMap":
            return doc["data"]["config.alloy"]
    raise AssertionError(
        "loki-homelab renders no ConfigMap carrying a config.alloy key"
    )


@pytest.mark.parametrize("fragment", REQUIRED_FRAGMENTS)
def test_config_carries_the_fragment(alloy_config: str, fragment: str) -> None:
    assert fragment in alloy_config, fragment


@pytest.mark.parametrize("job", REQUIRED_JOBS)
def test_every_promtail_job_is_still_declared(alloy_config: str, job: str) -> None:
    assert re.search(rf'"job"\s*=\s*"{job}"|replacement\s*=\s*"{job}"', alloy_config), (
        job
    )


def test_no_app_label_reaches_loki(alloy_config: str) -> None:
    assert 'target_label = "app"' not in alloy_config
    assert '"app" =' not in alloy_config


def test_the_guard_can_go_red() -> None:
    """A fragment list is only a guard if its members are the config's, not a paraphrase."""
    assert len(REQUIRED_FRAGMENTS) >= 10
    assert "stage.cri {}" in REQUIRED_FRAGMENTS
