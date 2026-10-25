"""daniel-pi's Alloy runs through the Pi's Compose project (H5.8).

Three guards moved here with the service from `ansible/tests/services/`
(`test_alloy_pi_config_labels.py`, `test_alloy_pi_http_surface.py` and
`test_alloy_pi_gomemlimit_headroom.py`). Each reads the rendered package of
`hosts/daniel-pi/package.yml`, which is what the Pi installs, never a template's source text.

- **Labels.** `container`, `job`, `machine` and `stream` are the contract that lets one LogQL query
  span the estate: the loki-homelab rule `LokiPiLogsSilent` selects `{job="pi"}`, and
  `probe.py alerts` reads `{job="syslog"} |= "status=down"` and relies on `machine="daniel-pi"`.
  The config is River, which no YAML guard can parse, so these read the rendered config as text.
- **HTTP surface.** The listener is LAN-published, so two flags drop /debug/pprof and /-/support,
  an `http.auth` block puts every other path behind basic auth, and
  `authenticate_matching_paths = false` inverts the filter. Flip that last one and the config
  still parses while /metrics needs a credential Prometheus does not send.
- **GOMEMLIMIT.** The limit bounds the Go runtime's total memory, and a limit under the heap the
  runtime needs makes it collect continuously at a measured 0.28 of a core.
- **The secret reaches Alloy through the environment only.** The password is no longer in a 0640
  file. The config reads it with `sys.env` and carries no secret, and the compose file fills the
  variable from the project's Compose env file (operator ruling in h5-pi-compose.md *Open
  questions* 6).

Run: uv run pytest hosts/lib/tests/test_pi_compose_alloy.py
"""

import hashlib
import re

import pytest
from _compose_tree import COMPOSE, project_of, text

from renderer.renderer_lib import host as host_mode

CONFIG = f"{COMPOSE}/alloy/config.alloy"
COMPOSE_FILE = f"{COMPOSE}/alloy/compose.yaml"
PASSWORD_ENV = "ALLOY_PI_HTTP_PASSWORD"

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
    # crowdsec's edge probe, counted by status for its off-cluster verify and never shipped.
    '"__path__" = "/var/log/pi-health/crowdsec-edge-probe.status"',
    'name              = "crowdsec_edge_probe_total"',
    'expression = "http=(?P<status>[0-9]{3})$"',
    # The push-only door on the LAN route. The domain arrives through the environment.
    'url = "https://loki-homelab.local." + sys.env("HOMELAB_DOMAIN") + "/loki/api/v1/push"',
)

# The flags whose defaults are ON in the pinned build (v1.19.2 internal/alloycli/cmd_run.go:
# enablePprof true, disableSupportBundle false).
REQUIRED_FLAGS = (
    "--server.http.enable-pprof=false",
    "--server.http.disable-support-bundle",
)

# The post-GC floor of `go_memstats_heap_alloc_bytes{job="alloy-pi"}`: 38.3-38.5 MB steady,
# after a three-day climb from 29 MB. Re-measure with
# `min_over_time(go_memstats_heap_alloc_bytes{job="alloy-pi"}[1d])` on a process older than
# three days, never on a fresh one (22 MB in its first hour).
MEASURED_LIVE_HEAP_MIB = 37
# `go_memstats_sys_bytes` − `go_memstats_heap_sys_bytes`: stacks, GC metadata, mspan/mcache
# and the profiling buckets. 8.3 MB.
NON_HEAP_RUNTIME_MIB = 8
# With GOGC=off the limit is the only trigger, so the slack between the live set and the
# limit is the whole cycle. Less than GOGC=50's half-heap of slack would give a shorter cycle
# than GOGC=50 does. The forced two-minute cycle GOGC=off removes is why it is off at all.
MIN_SLACK_PERCENT_WHEN_OFF = 50
_UNITS_MIB = {"MiB": 1, "M": 1, "GiB": 1024, "G": 1024}


@pytest.fixture(scope="module")
def package():
    return host_mode.render_host("daniel-pi")


@pytest.fixture(scope="module")
def config(package) -> str:
    return text(package, CONFIG)


@pytest.fixture(scope="module")
def compose(package) -> str:
    return text(package, COMPOSE_FILE)


@pytest.fixture(scope="module")
def service(package) -> dict:
    return project_of(package)["files"][COMPOSE_FILE]["services"]["alloy"]


def _env(service: dict) -> dict[str, str]:
    return dict(item.partition("=")[::2] for item in service["environment"])


# --- labels -----------------------------------------------------------------------------------


@pytest.mark.parametrize("fragment", REQUIRED_FRAGMENTS)
def test_config_carries_the_fragment(config, fragment):
    assert fragment in config, fragment


def test_container_streams_are_job_pi_on_machine_daniel_pi(config):
    assert re.search(r'target_label = "job"\s*\n\s*replacement  = "pi"', config)
    assert re.search(
        r'target_label = "machine"\s*\n\s*replacement  = "daniel-pi"', config
    )


def test_the_journal_is_not_shipped(config):
    """The service file's header records why: RSS, not line volume.

    The shipper peaks at 75-82 MB every day against a 96 MiB cap, and a journal reader's own
    footprint on top of that is unmeasured. Enabling it is a deliberate decision that starts
    with raising the cap, so this fails the moment someone adds the block without also
    deleting this test and the header's reasoning.
    """
    assert "loki.source.journal" not in re.sub(r"//[^\n]*", "", config)


def test_the_guard_can_go_red():
    assert len(REQUIRED_FRAGMENTS) >= 8
    assert '"machine" = "daniel-pi"' in REQUIRED_FRAGMENTS
    # A fragment that still carried a Jinja expression would match nothing in a render.
    assert not [f for f in REQUIRED_FRAGMENTS if "{{" in f]


# --- storage and resolver ---------------------------------------------------------------------


def test_storage_path_is_not_under_the_images_own_var_lib_alloy(compose, service):
    """The image's /var/lib/alloy is 0770 uid 473; uid 1000 cannot traverse it.

    A bind mount inside it is unreachable and Alloy dies at startup with
    `mkdir /var/lib/alloy/data: permission denied`.
    """
    assert "--storage.path=/data" in service["command"]
    binds = {(v["source"], v["target"]) for v in service["volumes"]}
    assert ("./data", "/data") in binds
    assert "/var/lib/alloy/data" not in re.sub(r"#[^\n]*", "", compose)


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


def test_resolver_retries_the_embedded_dns_hop(compose):
    """dockerd answers 127.0.0.11 and stalls for seconds under a container operation.

    A single 2s try fails every lookup in that window at level=error; see the comment above
    `dns_opt` in the service file.
    """
    assert _resolver_attempts(compose) >= 3


def test_resolver_attempts_reads_the_real_value_and_defaults_without_it():
    assert (
        _resolver_attempts("    dns_opt:\n      - timeout:3\n      - attempts:3\n") == 3
    )
    assert _resolver_attempts("    volumes:\n      - ./data:/data\n") == 1
    # A commented-out block is not a setting.
    assert _resolver_attempts("    # dns_opt:\n    #   - attempts:3\n") == 1


# --- HTTP surface -----------------------------------------------------------------------------


def compose_gaps(body: str) -> list[str]:
    """Return the hardening properties `body` (a rendered compose file) lacks."""
    gaps = [flag for flag in REQUIRED_FLAGS if flag not in body]
    if ":12345:12345/tcp" not in body:
        # Not a hardening property but the constraint the hardening exists to live with:
        # the two off-box consumers need the published mapping.
        gaps.append("published port")
    return gaps


def config_gaps(body: str) -> list[str]:
    """Return the auth properties the Alloy config lacks."""
    gaps = []
    if "http {" not in body or "auth {" not in body or "basic {" not in body:
        gaps.append("auth block")
    # The password is a SOPS value that reaches the container as an environment variable, so
    # this keys on the field reading that variable, never on a value.
    if not re.search(rf'password\s*=\s*sys\.env\("{PASSWORD_ENV}"\)', body):
        gaps.append("password from the environment")
    if '["/metrics"]' not in body:
        gaps.append("metrics exemption")
    if "authenticate_matching_paths = false" not in body:
        # `true` (the Alloy default) authenticates the LISTED paths, which would put
        # /metrics behind auth and leave the reload endpoint and the UI open.
        gaps.append("inverted filter")
    return gaps


def test_the_real_compose_file_is_clean(compose):
    assert compose_gaps(compose) == []


def test_a_compose_file_missing_a_flag_is_flagged(compose):
    stripped = compose.replace(REQUIRED_FLAGS[0], "")
    assert compose_gaps(stripped) == [REQUIRED_FLAGS[0]]


def test_a_compose_file_that_stopped_publishing_is_flagged(compose):
    # Binding to loopback is the obvious "fix" and it silently blinds the `alloy-pi` scrape job
    # and monitor-bridge's detached arm. The whole published token goes, host IP included: the
    # rendered file names the Pi's LAN address, so replacing the port halves alone would leave a
    # mapping no operator would ever write.
    unpublished = re.sub(
        r'"[\d.]+:12345:12345/tcp"', '"127.0.0.1:12345:12345"', compose
    )
    assert unpublished != compose
    assert compose_gaps(unpublished) == ["published port"]


def test_the_real_config_is_clean(config):
    assert config_gaps(config) == []


def test_a_config_without_the_auth_block_is_flagged(config):
    start = config.index("// The HTTP server on 12345")
    end = config.index("loki.write", start)
    assert config_gaps(config[:start] + config[end:]) == [
        "auth block",
        "password from the environment",
        "metrics exemption",
        "inverted filter",
    ]


def test_a_config_whose_filter_is_not_inverted_is_flagged(config):
    flipped = config.replace(
        "authenticate_matching_paths = false", "authenticate_matching_paths = true"
    )
    assert config_gaps(flipped) == ["inverted filter"]


def test_a_config_whose_password_is_blank_is_flagged(config):
    """An emptied `password = ""` still parses and leaves the control surface open."""
    blanked = re.sub(r"password\s*=\s*sys\.env\([^)]*\)", 'password = ""', config)
    assert blanked != config
    assert config_gaps(blanked) == ["password from the environment"]


# --- the secret --------------------------------------------------------------------------------


def test_the_password_reaches_alloy_through_the_compose_env_file_key(
    package, config, service
):
    assert _env(service)[PASSWORD_ENV] == "${alloy_pi_http_password}"
    assert "alloy_pi_http_password" in {k for o in package.outputs for k in o["keys"]}
    # The config is a plain file at 0644, so it must hold no placeholder and no credential.
    _, mode = package.files[CONFIG]
    assert mode == 0o644
    assert "${" not in config and "alloy_pi_http_password" not in config


def test_the_domain_is_a_plain_variable_rendered_at_build_time(service):
    domain = _env(service)["HOMELAB_DOMAIN"]
    assert re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]+", domain), domain


def test_a_config_edit_recreates_alloy_through_its_label(package, service):
    """Compose cannot see a bind-mounted file's content; the label is what changes on an edit."""
    digest = hashlib.sha256(package.files[CONFIG][0]).hexdigest()
    assert f"homelab.config-sha256={digest}" in service["labels"]


def test_alloy_waits_for_the_read_only_proxy_it_discovers_through(service):
    assert "docker-proxy" in list(service["depends_on"])
    assert service["networks"] == ["proxy"]


# --- GOMEMLIMIT --------------------------------------------------------------------------------


def _mib(quantity: str) -> int:
    match = re.fullmatch(r"(\d+)(MiB|M|GiB|G)", quantity)
    assert match, f"unparseable memory quantity {quantity!r}"
    return int(match.group(1)) * _UNITS_MIB[match.group(2)]


def gomemlimit_problem(gomemlimit: str, mem_cap: str, gogc: int | str) -> str | None:
    """The failure message for a (GOMEMLIMIT, compose memory cap, GOGC) triple, else None.

    `gogc` is the percentage, or the string "off"; anything else is a malformed compose.
    """
    limit = _mib(gomemlimit)
    if gogc == "off":
        heap_goal = MEASURED_LIVE_HEAP_MIB * (100 + MIN_SLACK_PERCENT_WHEN_OFF) // 100
        how = f"GOGC=off needs at least {MIN_SLACK_PERCENT_WHEN_OFF}% slack, a {heap_goal} MiB goal"
    else:
        assert isinstance(gogc, int), (
            f"GOGC must be a percentage or 'off', not {gogc!r}"
        )
        heap_goal = MEASURED_LIVE_HEAP_MIB * (100 + gogc) // 100
        how = f"at GOGC={gogc} is a {heap_goal} MiB goal"
    floor = heap_goal + NON_HEAP_RUNTIME_MIB
    if limit < floor:
        return (
            f"GOMEMLIMIT={gomemlimit} is under the {floor} MiB floor (live heap "
            f"{MEASURED_LIVE_HEAP_MIB} MiB {how}, plus "
            f"{NON_HEAP_RUNTIME_MIB} non-heap); the limit sets the heap goal, not GOGC"
        )
    if limit >= _mib(mem_cap):
        return f"GOMEMLIMIT={gomemlimit} is not below the {mem_cap} memory cap; OOM before GC"
    return None


def test_the_live_limit_has_headroom_and_a_cap_above_it(service):
    env = _env(service)
    cap = service["deploy"]["resources"]["limits"]["memory"]
    assert {"GOMEMLIMIT", "GOGC"} <= set(env) and cap, (
        f"the alloy container lost its GOMEMLIMIT, GOGC or memory limit: {env}, cap {cap!r}"
    )
    gogc = env["GOGC"] if env["GOGC"] == "off" else int(env["GOGC"])
    assert gomemlimit_problem(env["GOMEMLIMIT"], cap, gogc) is None


def test_the_shipped_triple_is_clean():
    assert gomemlimit_problem("104MiB", "128M", "off") is None


def test_a_limit_with_too_little_slack_for_gogc_off_is_flagged():
    """With GOGC off the limit is the only trigger; 60MiB leaves under half a heap of slack."""
    assert gomemlimit_problem("60MiB", "128M", "off") == (
        "GOMEMLIMIT=60MiB is under the 63 MiB floor (live heap 37 MiB GOGC=off needs at "
        "least 50% slack, a 55 MiB goal, plus 8 non-heap); the limit sets the heap goal, "
        "not GOGC"
    )


def test_the_2026_09_03_triple_is_clean():
    """The GOGC=50 pairing: a 72MiB limit clears the 63 MiB floor."""
    assert gomemlimit_problem("72MiB", "96M", 50) is None


def test_the_2026_09_02_value_is_flagged():
    """A 48MiB limit, sized from RSS rather than the heap goal, is under the floor."""
    assert gomemlimit_problem("48MiB", "96M", 50) == (
        "GOMEMLIMIT=48MiB is under the 63 MiB floor (live heap 37 MiB at GOGC=50 is a "
        "55 MiB goal, plus 8 non-heap); the limit sets the heap goal, not GOGC"
    )


def test_the_default_gogc_moves_the_floor_past_the_limit():
    """Dropping the GOGC line restores Go's default 100%, and 72MiB no longer clears it."""
    assert gomemlimit_problem("72MiB", "96M", 100) == (
        "GOMEMLIMIT=72MiB is under the 82 MiB floor (live heap 37 MiB at GOGC=100 is a "
        "74 MiB goal, plus 8 non-heap); the limit sets the heap goal, not GOGC"
    )


def test_a_limit_at_the_cap_is_flagged():
    assert gomemlimit_problem("96MiB", "96M", 50) == (
        "GOMEMLIMIT=96MiB is not below the 96M memory cap; OOM before GC"
    )
