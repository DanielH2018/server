"""The OTEL resource attributes a fan-out batch's `claude -p` runs under.

Claude Code's telemetry reaches Prometheus with each resource attribute as a label, dots
turned to underscores. `session_kind` splits spend, tokens and tool failures by what started
a session, and `fanout_batch` and `fanout_phase` split a fan-out batch's further.

Claude Code applies a settings `env` over the process environment and replaces the whole
`OTEL_RESOURCE_ATTRIBUTES` string, so each fan-out launcher passes the value both in the
environment and in `--settings` (`review.held_hooks.labelled`).
"""

import json
import os
from enum import StrEnum
from pathlib import Path


class SessionKind(StrEnum):
    """The `session.kind` values. A session no launcher labels carries no such label.

    `REMOTE_CONTROL` and `BG_RESUME` are set by the claude-code host component's
    `claude-rc.service.j2` and `claude-bg-resume.service.j2`. `TRIAGE` and `RENOVATE` are set
    by the unattended sessions' settings overlays, `claude-triage-settings-overlay.json` and
    the renovate-agent component's `claude-settings-overlay.json`.
    """

    FANOUT_BATCH = "fanout-batch"
    FANOUT_REVIEW = "fanout-review"
    REMOTE_CONTROL = "remote-control"
    BG_RESUME = "bg-resume"
    TRIAGE = "triage"
    RENOVATE = "renovate"


def batch_attributes(batch: str) -> str:
    """The `OTEL_RESOURCE_ATTRIBUTES` value a plain batch's `claude -p` runs under.

    Unlike `otel_attributes`, it merges nothing: the placing host builds it, and the unit
    may run on another host, whose settings the placing host cannot read.
    """
    return f"session.kind={SessionKind.FANOUT_BATCH},fanout.batch={batch}"


def otel_attributes(batch: str, phase: str) -> str:
    """The `OTEL_RESOURCE_ATTRIBUTES=` assignment one review phase's `claude -p` runs under.

    An attribute the unit's own environment or the user's settings already carries is kept
    ahead of the batch's. The settings value matters because the agent user's
    `process.owner=claude` would otherwise be displaced by the `--settings` copy.
    """
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        from_settings = json.loads((config / "settings.json").read_text())["env"][
            "OTEL_RESOURCE_ATTRIBUTES"
        ]
    except OSError, ValueError, KeyError, TypeError:
        from_settings = ""
    attributes: dict[str, str] = {}
    for pair in (
        *str(from_settings).split(","),
        *os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","),
        f"session.kind={SessionKind.FANOUT_REVIEW}",
        f"fanout.batch={batch}",
        f"fanout.phase={phase}",
    ):
        key, _, value = pair.partition("=")
        if key.strip():
            attributes[key.strip()] = value
    return "OTEL_RESOURCE_ATTRIBUTES=" + ",".join(
        f"{k}={v}" for k, v in attributes.items()
    )
