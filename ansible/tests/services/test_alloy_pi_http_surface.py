#!/usr/bin/env python3
"""The Pi's Alloy HTTP listener is LAN-published, so its surface must stay narrowed.

daniel-pi's Alloy publishes 12345 on the LAN IP because two consumers reach it from off-box:
claude-otel's `alloy-pi` Prometheus job (a STATIC target, so it cannot use a loopback or a
bridge address) and monitor-bridge's detached-container arm, which expects `alloy` to report a
published mapping. Closing the port is therefore not available — issue #1130 was closed by
narrowing what answers on it instead.

Three things hold that narrowing, and each can be undone by an edit that looks harmless:
the two `--server.http.*` flags that drop /debug/pprof and /-/support, the `http.auth` block
that puts every other path behind basic auth, and the `authenticate_matching_paths = false`
that INVERTS the filter. Flip that last one to true and the config still parses, Alloy still
starts, and the result is the exact inverse: /metrics needs a credential Prometheus does not
send and everything else is open. That failure is silent on the repo side, which is why it is
pinned here rather than left to review.

Every check is a `..._is_clean` / `..._is_flagged` pair over the same reader, so a reader that
stopped matching anything fails its own test.

Run: uv run pytest ansible/tests/services/test_alloy_pi_http_surface.py
"""

from _helpers import ANSIBLE

ROLE = ANSIBLE / "roles" / "containers" / "alloy"
COMPOSE = ROLE / "templates" / "docker-compose.yml.j2"
CONFIG = ROLE / "templates" / "config.alloy.j2"
TASKS = ROLE / "tasks" / "main.yml"

# The flags whose defaults are ON in the pinned build (v1.19.2 internal/alloycli/cmd_run.go:
# enablePprof true, disableSupportBundle false).
REQUIRED_FLAGS = (
    "--server.http.enable-pprof=false",
    "--server.http.disable-support-bundle",
)


def compose_gaps(text: str) -> list[str]:
    """Return the hardening properties `text` (a rendered-or-raw compose template) lacks."""
    gaps = [flag for flag in REQUIRED_FLAGS if flag not in text]
    if ":12345:12345/tcp" not in text:
        # Not a hardening property but the constraint the hardening exists to live with:
        # the two off-box consumers need the published mapping.
        gaps.append("published port")
    return gaps


def config_gaps(text: str) -> list[str]:
    """Return the auth properties the Alloy config template lacks."""
    gaps = []
    if "http {" not in text or "auth {" not in text or "basic {" not in text:
        gaps.append("auth block")
    if "{{ alloy_pi_http_password }}" not in text:
        gaps.append("password from SOPS")
    if '["/metrics"]' not in text:
        gaps.append("metrics exemption")
    if "authenticate_matching_paths = false" not in text:
        # `true` (the Alloy default) authenticates the LISTED paths, which would put
        # /metrics behind auth and leave the reload endpoint and the UI open.
        gaps.append("inverted filter")
    return gaps


def test_the_real_compose_template_is_clean():
    assert compose_gaps(COMPOSE.read_text()) == []


def test_a_compose_template_missing_a_flag_is_flagged():
    stripped = COMPOSE.read_text().replace(REQUIRED_FLAGS[0], "")
    assert compose_gaps(stripped) == [REQUIRED_FLAGS[0]]


def test_a_compose_template_that_stopped_publishing_is_flagged():
    # Binding to loopback is the obvious "fix" for #1130 and it silently blinds the
    # `alloy-pi` scrape job and monitor-bridge's detached arm.
    unpublished = COMPOSE.read_text().replace(
        ":12345:12345/tcp", "127.0.0.1:12345:12345"
    )
    assert compose_gaps(unpublished) == ["published port"]


def test_the_real_config_template_is_clean():
    assert config_gaps(CONFIG.read_text()) == []


def test_a_config_without_the_auth_block_is_flagged():
    text = CONFIG.read_text()
    start = text.index("// The HTTP server on 12345")
    end = text.index("loki.write", start)
    assert config_gaps(text[:start] + text[end:]) == [
        "auth block",
        "password from SOPS",
        "metrics exemption",
        "inverted filter",
    ]


def test_a_config_whose_filter_is_not_inverted_is_flagged():
    flipped = CONFIG.read_text().replace(
        "authenticate_matching_paths = false", "authenticate_matching_paths = true"
    )
    assert config_gaps(flipped) == ["inverted filter"]


def test_the_config_file_is_not_world_readable():
    # It carries the basic-auth password, so 0644 would hand it to every account on the Pi.
    assert 'mode: "0640"' in TASKS.read_text()
    assert 'mode: "0644"' not in TASKS.read_text()
