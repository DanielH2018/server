#!/usr/bin/env python3
"""Guard that every descriptive-`down` Uptime-Kuma PUSH monitor sets max_retries=0.

monitor-bridge and the host-cron watchdogs push `status=down&msg=<named offender>` to their Kuma
push monitors. With max_retries>0 Kuma parks a pushed `down` in PENDING and its 60s heartbeat
watchdog — which only an `up` push satisfies — crosses maxretries first, so the visible DOWN event
reads "No heartbeat in the time window" instead of the check's descriptive msg (the 2026-06-12 bug,
re-triggered 2026-07-12 when three monitors silently inherited the kuma() macro's default
max_retries=2). Every monitor-bridge/state-file push monitor is descriptive-`down`, so every one
must set max_retries=0.

The kuma() macro (ansible/templates/autokuma.yml.j2) defaults max_retries=2, so a push monitor that
omits it regresses with no other signal. This asserts every monitor_type='push' kuma() call across
the deployed compose templates sets max_retries=0 — except the up-only dead-men (cloudflare-ddns),
which only ever push `up` (pure heartbeat-silence monitors) so a couple of retries are harmless.

Run: uv run pytest scripts/test_push_monitor_retries.py
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_COMPOSE_GLOB = "ansible/roles/containers/*/templates/docker-compose.yml.j2"

# Up-only dead-man push monitors that only ever push `up` (pure heartbeat-silence, no descriptive
# `down`), so the max_retries=0 rule doesn't apply — a couple of retries there is harmless.
_UP_ONLY_ALLOWLIST = frozenset(
    {"cloudflare-ddns-direct-push", "cloudflare-ddns-proxied-push"}
)

# Every push monitor's kuma() call is a single line: `{{ kuma('slug', monitor_type='push', ...) }}`.
_KUMA_SLUG = re.compile(r"kuma\(\s*'([^']+)'")


def _push_monitors() -> list[tuple[str, str, str]]:
    """(compose_path, slug, line) for every monitor_type='push' kuma() call in the repo."""
    found = []
    for path in sorted(_REPO.glob(_COMPOSE_GLOB)):
        for line in path.read_text().splitlines():
            if "monitor_type='push'" not in line:
                continue
            m = _KUMA_SLUG.search(line)
            if m:
                found.append((path.relative_to(_REPO).as_posix(), m.group(1), line))
    return found


def test_push_monitors_are_discovered():
    # Guard the guard: if the macro-call format drifts and the regex matches nothing, the real
    # assertion below would pass vacuously. Pin a floor well under today's count (~41).
    monitors = _push_monitors()
    assert len(monitors) >= 35, (
        "found only %d push monitors — regex/format drift?" % len(monitors)
    )


def test_every_descriptive_down_push_monitor_sets_max_retries_zero():
    offenders = [
        "%s: %s" % (path, slug)
        for path, slug, line in _push_monitors()
        if slug not in _UP_ONLY_ALLOWLIST and "max_retries=0" not in line
    ]
    assert not offenders, (
        "push monitor(s) missing max_retries=0 — they inherit the kuma() default of 2, so Kuma "
        "masks the descriptive down as 'No heartbeat in the time window' (the 2026-06-12 bug). Set "
        "max_retries=0, or add to _UP_ONLY_ALLOWLIST if the monitor only ever pushes `up`:\n  "
        + "\n  ".join(offenders)
    )
