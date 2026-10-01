#!/usr/bin/env python3
"""autoheal must carry no healthcheck, or it restarts itself under load and stays down.

`AUTOHEAL_CONTAINER_LABEL=all` watches every container that reports `unhealthy`, with no
self-exclusion (the entrypoint's filter is `health=unhealthy` plus an optional label). The
image bakes `pgrep -f autoheal` as its own probe, so while autoheal has a healthcheck it is
its own patient. On the 512 MB Pi a deploy's memory thrash stalls that exec past its timeout,
autoheal reads itself unhealthy, restarts itself, and the `start` fails at the OCI runtime —
a failed start does not fire `restart: unless-stopped`, so it sits Exited until the recovery
cron's `docker start`. Observed 2026-09-04 18:47Z in autoheal's own log at the window's load
peak; the same signature fits the 2026-08-29 and 2026-09-02/03 stops (#1789).

A healthcheck block that only says `disable: true` is the accepted shape. Anything that
gives Docker a probe to run — `test:`, an `interval:` that inherits the image's baked test,
or no `healthcheck:` key at all (which also inherits it) — is the regression.

The check reads the RENDERED service, not the template text (#2809). A text scan passed a
template whose whole `healthcheck:` block sat inside a Jinja `{# … #}` comment, which renders
no healthcheck at all and so inherits the image's probe.

Run: uv run pytest ansible/tests/services/test_autoheal_does_not_watch_itself.py
"""

from _compose_render import render_service

# The keys that hand Docker a probe. `interval:` counts: with `test:` omitted, Docker runs
# the image's own HEALTHCHECK at that cadence, which is exactly the pre-#1789 shape.
_PROBE_KEYS = ("test", "interval", "timeout", "retries", "start_period")


def self_watch_gaps(service: dict) -> list[str]:
    """Why a rendered compose service lets autoheal probe itself. Empty when it cannot."""
    healthcheck = service.get("healthcheck")
    if healthcheck is None:
        return ["no healthcheck: key, so the image's baked pgrep probe is inherited"]
    gaps: list[str] = []
    if healthcheck.get("disable") is not True:
        gaps.append("healthcheck: block lacks `disable: true`")
    gaps.extend(
        f"healthcheck carries `{key}:`, which hands Docker a probe to run"
        for key in _PROBE_KEYS
        if key in healthcheck
    )
    return gaps


def test_the_shipped_template_gives_autoheal_no_probe() -> None:
    assert self_watch_gaps(render_service("autoheal")) == []


def test_an_interval_only_override_is_flagged() -> None:
    """The rejecting half: the exact pre-#1789 shape, `test` omitted and the image's inherited."""
    gaps = self_watch_gaps({"healthcheck": {"interval": "60s"}})
    assert any("interval" in g for g in gaps), gaps
    assert any("disable" in g for g in gaps), gaps


def test_a_missing_healthcheck_key_is_flagged() -> None:
    assert self_watch_gaps({"environment": ["AUTOHEAL_CONTAINER_LABEL=all"]}) == [
        "no healthcheck: key, so the image's baked pgrep probe is inherited"
    ]
