"""daniel-pi's Alloy config must emit the labels the cluster's streams carry.

`container`, `job`, `machine` and `stream` are the contract that lets one LogQL query span
the estate: monitor-bridge's `LOKI_PI_STREAM` selects `{job="pi"}`, `probe.py alerts` reads
`{job="syslog"} |= "status=down"` and relies on `machine="daniel-pi"` to tell the Pi's health
crons from the cluster hosts' syslog. The config is River, which `validate/config_templates.py`
cannot parse, so this reads the template text.
"""

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


_COMPOSE = REPO / "ansible/roles/containers/alloy/templates/docker-compose.yml.j2"


def test_storage_path_is_not_under_the_images_own_var_lib_alloy() -> None:
    """The image's /var/lib/alloy is 0770 uid 473; uid 1000 cannot traverse it.

    A bind mount inside it is unreachable and Alloy dies at startup with
    `mkdir /var/lib/alloy/data: permission denied` — the first deploy of this role did
    exactly that, every two minutes, while the spike (root) had run for an hour.
    """
    compose = _COMPOSE.read_text()
    assert "--storage.path=/data" in compose
    assert "./data:/data" in compose
    assert "/var/lib/alloy/data" not in re.sub(r"#[^\n]*", "", compose)


def test_the_guard_can_go_red() -> None:
    assert len(REQUIRED_FRAGMENTS) >= 8
    assert '"machine" = "daniel-pi"' in REQUIRED_FRAGMENTS


_DNS_OPT_BLOCK = re.compile(r"^\s+dns_opt:\n((?:\s+- \S+\n)+)", re.MULTILINE)


def _resolver_attempts(compose: str) -> int:
    """Return the `attempts:` value the compose's `dns_opt` sets, or 1 (resolv.conf's default).

    Docker copies the host's `timeout:2 attempts:1` into the container otherwise, and one 2s
    try against the embedded resolver is what issue #927 measured failing.
    """
    block = _DNS_OPT_BLOCK.search(re.sub(r"#[^\n]*", "", compose))
    if block is None:
        return 1
    found = re.search(r"- attempts:(\d+)", block.group(1))
    return int(found.group(1)) if found else 1


def test_resolver_retries_the_embedded_dns_hop() -> None:
    """dockerd answers 127.0.0.11 and stalls for seconds under a container operation.

    A single 2s try fails every lookup in that window at level=error; see the comment above
    `dns_opt` in the compose template.
    """
    assert _resolver_attempts(_COMPOSE.read_text()) >= 3


def test_resolver_attempts_reads_the_real_value_and_defaults_without_it() -> None:
    assert (
        _resolver_attempts("    dns_opt:\n      - timeout:3\n      - attempts:3\n") == 3
    )
    assert _resolver_attempts("    volumes:\n      - ./data:/data\n") == 1
    # A commented-out block is not a setting.
    assert _resolver_attempts("    # dns_opt:\n    #   - attempts:3\n") == 1
