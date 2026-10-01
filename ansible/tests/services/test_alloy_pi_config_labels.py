"""daniel-pi's Alloy config must emit the labels the cluster's streams carry.

`container`, `job`, `machine` and `stream` are the contract that lets one LogQL query span
the estate: monitor-bridge's `LOKI_PI_STREAM` selects `{job="pi"}`, `probe.py alerts` reads
`{job="syslog"} |= "status=down"` and relies on `machine="daniel-pi"` to tell the Pi's health
crons from the cluster hosts' syslog. The config is River, which `validate/config_templates.py`
cannot parse, so this reads the RENDERED config as text — what the container receives, with
`{{ domain }}` already expanded (#3175).
"""

import re

import pytest

from _compose_render import host_context, rendered_text

# The render's own domain, not a hardcoded one: the push URL is the only fragment below that
# carries a variable, and pinning a literal domain here would check a value the inventory may
# not hold.
_DOMAIN = host_context()["domain"]

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
    f'url = "https://loki-homelab.local.{_DOMAIN}/loki/api/v1/push"',
)


@pytest.fixture(scope="module")
def alloy_config() -> str:
    return rendered_text("alloy", "config.alloy.j2")


@pytest.mark.parametrize("fragment", REQUIRED_FRAGMENTS)
def test_config_carries_the_fragment(alloy_config: str, fragment: str) -> None:
    assert fragment in alloy_config, fragment


def test_container_streams_are_job_pi_on_machine_daniel_pi(alloy_config: str) -> None:
    assert re.search(r'target_label = "job"\s*\n\s*replacement  = "pi"', alloy_config)
    assert re.search(
        r'target_label = "machine"\s*\n\s*replacement  = "daniel-pi"', alloy_config
    )


def test_the_journal_is_not_shipped(alloy_config: str) -> None:
    """The role's CLAUDE.md records why: RSS, not line volume.

    The shipper peaks at 75-82 MB every day against a 96 MiB cap, and a journal reader's own
    footprint on top of that is unmeasured. Enabling it is a deliberate decision that starts
    with raising the cap, so this fails the moment someone adds the block without also
    deleting this test and the CLAUDE.md reasoning.
    """
    assert "loki.source.journal" not in re.sub(r"//[^\n]*", "", alloy_config)


def test_storage_path_is_not_under_the_images_own_var_lib_alloy() -> None:
    """The image's /var/lib/alloy is 0770 uid 473; uid 1000 cannot traverse it.

    A bind mount inside it is unreachable and Alloy dies at startup with
    `mkdir /var/lib/alloy/data: permission denied`.
    """
    compose = rendered_text("alloy", "docker-compose.yml.j2")
    assert "--storage.path=/data" in compose
    assert "./data:/data" in compose
    assert "/var/lib/alloy/data" not in re.sub(r"#[^\n]*", "", compose)


def test_the_guard_can_go_red() -> None:
    assert len(REQUIRED_FRAGMENTS) >= 8
    assert '"machine" = "daniel-pi"' in REQUIRED_FRAGMENTS
    # A fragment that still carried a Jinja expression would match nothing in a render.
    assert not [f for f in REQUIRED_FRAGMENTS if "{{" in f]


_DNS_OPT_BLOCK = re.compile(r"^\s+dns_opt:\n((?:\s+- \S+\n)+)", re.MULTILINE)


def _resolver_attempts(compose: str) -> int:
    """Return the `attempts:` value the compose's `dns_opt` sets, or 1 (resolv.conf's default).

    Docker copies the host's `timeout:2 attempts:1` into the container otherwise, and one 2s
    try against the embedded resolver fails.
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
    assert _resolver_attempts(rendered_text("alloy", "docker-compose.yml.j2")) >= 3


def test_resolver_attempts_reads_the_real_value_and_defaults_without_it() -> None:
    assert (
        _resolver_attempts("    dns_opt:\n      - timeout:3\n      - attempts:3\n") == 3
    )
    assert _resolver_attempts("    volumes:\n      - ./data:/data\n") == 1
    # A commented-out block is not a setting.
    assert _resolver_attempts("    # dns_opt:\n    #   - attempts:3\n") == 1
