"""daniel-pi's Alloy config must emit the labels the cluster's streams carry.

`container`, `job`, `machine` and `stream` are the contract that lets one LogQL query span
the estate: monitor-bridge's `LOKI_PI_STREAM` selects `{job="pi"}`, `probe.py alerts` reads
`{job="syslog"} |= "status=down"` and relies on `machine="daniel-pi"` to tell the Pi's health
crons from the cluster hosts' syslog. The config is River, which `validate/config_templates.py`
cannot parse, so this reads the template text.
"""

from __future__ import annotations

import re

import pytest

from _helpers import REPO

_CONFIG = REPO / "ansible/roles/containers/alloy/templates/config.alloy.j2"

REQUIRED_FRAGMENTS = (
    # Discovery through the read-only proxy, never the raw socket.
    'host = "tcp://docker-proxy:2375"',
    # Docker's leading slash stripped, so `container` matches the cluster's label.
    'regex         = "/(.*)"',
    'target_label  = "container"',
    'target_label  = "stream"',
    # The pi-health crons' verdict lines, under the cluster hosts' syslog job.
    '"__path__" = "/var/log/pi-health/*.log"',
    '"job" = "syslog"',
    '"machine" = "daniel-pi"',
    # The push-only door on the LAN route.
    'url = "https://loki-homelab.local.{{ domain }}/loki/api/v1/push"',
)


@pytest.fixture(scope="module")
def alloy_config() -> str:
    return _CONFIG.read_text()


@pytest.mark.parametrize("fragment", REQUIRED_FRAGMENTS)
def test_config_carries_the_fragment(alloy_config: str, fragment: str) -> None:
    assert fragment in alloy_config, fragment


def test_container_streams_are_job_pi_on_machine_daniel_pi(alloy_config: str) -> None:
    assert re.search(r'target_label = "job"\s*\n\s*replacement  = "pi"', alloy_config)
    assert re.search(
        r'target_label = "machine"\s*\n\s*replacement  = "daniel-pi"', alloy_config
    )


def test_the_journal_is_not_shipped(alloy_config: str) -> None:
    """The header records why: ~38k lines/day for a signal pi-health carries in ~576.

    Enabling it is a deliberate decision about Loki volume, so this fails the moment someone
    adds the block without also deleting this test and the header's reasoning.
    """
    assert "loki.source.journal" not in re.sub(r"//[^\n]*", "", alloy_config)


def test_the_guard_can_go_red() -> None:
    assert len(REQUIRED_FRAGMENTS) >= 8
    assert '"machine" = "daniel-pi"' in REQUIRED_FRAGMENTS
