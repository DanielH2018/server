#!/usr/bin/env python3
"""The Pi's Alloy HTTP listener is LAN-published, so its surface must stay narrowed.

daniel-pi's Alloy publishes 12345 on the LAN IP because two consumers reach it from off-box:
observability's `alloy-pi` Prometheus job (a STATIC target, so it cannot use a loopback or a
bridge address) and monitor-bridge's detached-container arm, which expects `alloy` to report a
published mapping. Closing the port is therefore not available, so the hardening narrows
what answers on it instead.

Three things hold that narrowing, and each can be undone by an edit that looks harmless:
the two `--server.http.*` flags that drop /debug/pprof and /-/support, the `http.auth` block
that puts every other path behind basic auth, and the `authenticate_matching_paths = false`
that INVERTS the filter. Flip that last one to true and the config still parses, Alloy still
starts, and the result is the exact inverse: /metrics needs a credential Prometheus does not
send and everything else is open. That failure is silent on the repo side, which is why it is
pinned here rather than left to review.

Every check is a `..._is_clean` / `..._is_flagged` pair over the same reader, so a reader that
stopped matching anything fails its own test. Both readers run over the RENDERED file rather
than the template, so a value moved into a variable is still read (#3175).

Run: uv run pytest ansible/tests/services/test_alloy_pi_http_surface.py
"""

import re

from _compose_render import rendered_text
from _helpers import CONTAINER_ROLES

TASKS = CONTAINER_ROLES / "alloy" / "tasks" / "main.yml"


def compose() -> str:
    """The compose file the Pi's deploy writes, not the template that produces it (#3175)."""
    return rendered_text("alloy", "docker-compose.yml.j2")


def config() -> str:
    """The Alloy config the container reads, not the template that produces it."""
    return rendered_text("alloy", "config.alloy.j2")


# The flags whose defaults are ON in the pinned build (v1.19.2 internal/alloycli/cmd_run.go:
# enablePprof true, disableSupportBundle false).
REQUIRED_FLAGS = (
    "--server.http.enable-pprof=false",
    "--server.http.disable-support-bundle",
)


def compose_gaps(text: str) -> list[str]:
    """Return the hardening properties `text` (a rendered compose file) lacks."""
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
    # The password is a SOPS value, which renders as `STUB` — so this keys on the field
    # carrying SOMETHING, never on the value (the `DECIDED:` rule in `lib/render_guard.py`).
    if not re.search(r'password\s*=\s*"[^"]+"', text):
        gaps.append("password from SOPS")
    if '["/metrics"]' not in text:
        gaps.append("metrics exemption")
    if "authenticate_matching_paths = false" not in text:
        # `true` (the Alloy default) authenticates the LISTED paths, which would put
        # /metrics behind auth and leave the reload endpoint and the UI open.
        gaps.append("inverted filter")
    return gaps


def test_the_real_compose_file_is_clean():
    assert compose_gaps(compose()) == []


def test_a_compose_file_missing_a_flag_is_flagged():
    stripped = compose().replace(REQUIRED_FLAGS[0], "")
    assert compose_gaps(stripped) == [REQUIRED_FLAGS[0]]


def test_a_compose_file_that_stopped_publishing_is_flagged():
    # Binding to loopback is the obvious "fix" and it silently blinds the
    # `alloy-pi` scrape job and monitor-bridge's detached arm.
    unpublished = compose().replace(":12345:12345/tcp", "127.0.0.1:12345:12345")
    assert compose_gaps(unpublished) == ["published port"]


def test_the_real_config_is_clean():
    assert config_gaps(config()) == []


def test_a_config_without_the_auth_block_is_flagged():
    text = config()
    start = text.index("// The HTTP server on 12345")
    end = text.index("loki.write", start)
    assert config_gaps(text[:start] + text[end:]) == [
        "auth block",
        "password from SOPS",
        "metrics exemption",
        "inverted filter",
    ]


def test_a_config_whose_filter_is_not_inverted_is_flagged():
    flipped = config().replace(
        "authenticate_matching_paths = false", "authenticate_matching_paths = true"
    )
    assert config_gaps(flipped) == ["inverted filter"]


def test_a_config_whose_password_is_blank_is_flagged():
    """An emptied `password = ""` still parses and leaves the control surface open."""
    blanked = re.sub(r'password\s*=\s*"[^"]+"', 'password = ""', config())
    assert config_gaps(blanked) == ["password from SOPS"]


def test_the_config_file_is_not_world_readable():
    # It carries the basic-auth password, so 0644 would hand it to every account on the Pi.
    text = TASKS.read_text()
    # Scoped to the config task rather than asserting no 0644 anywhere in the role: an
    # unrelated file task added later is not this property going wrong.
    block = text[
        text.index("src: config.alloy.j2") : text.index("register: alloy_config")
    ]
    assert 'mode: "0640"' in block
