#!/usr/bin/env python3
"""Restart one allowlisted Deployment for homelab-claude-restart@<name>.service.

The unit runs this as root with the instance name as its only argument. The name is a key of
the allowlist in the config, and the namespace and Deployment come from that entry, never from
the name. The script refuses, in this order:

- `hold`: the hold file exists;
- `not-listed`: the name is not a key of the allowlist;
- `muted`: the weekly reboot's mute is open, so a failed restart would page nobody;
- `rate`: the workload was restarted inside its cooldown, or the day's cap is spent;
- `busy`: the Deployment is mid-rollout, or its Flux Kustomization is not Ready.

Otherwise it runs `kubectl rollout restart` with its own field manager, waits for the rollout,
and compares the Deployment's generation before and after, because kubectl prints success for
an identity that cannot write.

Every run ends in one `RESTART-RESULT:` logfmt line, in the journal and in `<state_dir>/verdict`.
A restart, done or failed, is also appended to `<state_dir>/log.jsonl` and posted to Discord.

Usage: claude_restart.py NAME
Exit 0 = restarted or refused, 1 = failed, 2 = a name outside the unit's shape.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lib.k8s_conditions import ConditionStatus, parse_conditions
from host_lib import (
    ERR,
    atomic_write,
    discord_post,
    install_excepthook,
    kubectl_runner,
    log,
    logfmt,
)
from weekly_window import in_weekly_window

CONFIG = "/etc/claude-restart/config.json"
TAG = "claude-restart"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# kustomize-controller takes over the fields of every manager whose name starts with `kubectl`
# and removes the ones its render does not declare, which would roll the pod a second time.
FIELD_MANAGER = "claude-restart"
READ_TIMEOUT_S = 30
FLUX_NAMESPACE = "flux-system"
FLUX_NAME_LABEL = "kustomize.toolkit.fluxcd.io/name"

Kubectl = Callable[..., tuple[int, str]]


@dataclass(frozen=True)
class Result:
    """How one request ended.

    Attributes:
        verdict: `restarted`, `refused` or `failed`.
        cause: the refusal's name, or a short word for the failure; empty for a restart.
        detail: one sentence for the operator, written only from the config and the clock.
    """

    verdict: str
    cause: str = ""
    detail: str = ""


def _utc(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%Y-%m-%dT%H:%MZ")


def past_restarts(state_dir: Path) -> list[dict]:
    """The restarts `log.jsonl` records, oldest first; a missing log is no restarts."""
    try:
        lines = (state_dir / "log.jsonl").read_text().splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def rate_refusal(name: str, cfg: dict, restarts: list[dict], now: float) -> str:
    """Why the rate limit refuses `name` now, or an empty string."""
    cooldown = cfg["workload_cooldown_s"]
    last = max((r["ts"] for r in restarts if r["name"] == name), default=None)
    if last is not None and now - last < cooldown:
        return f"{name} was restarted at {_utc(last)}; next allowed {_utc(last + cooldown)}"
    day = _utc(now)[:10]
    today = sum(1 for r in restarts if _utc(r["ts"])[:10] == day)
    if today >= cfg["daily_cap"]:
        return f"{today} restarts on {day} UTC, and the cap is {cfg['daily_cap']} a day"
    return ""


def read_deployment(kubectl: Kubectl, deployment: str) -> dict | None:
    """The live Deployment, or None when kubectl could not read it."""
    rc, out = kubectl("get", "deployment", deployment, "-o", "json")
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def busy_reason(live: dict, flux: Kubectl) -> str:
    """Why `live` must not be restarted now, or an empty string."""
    status = live.get("status", {})
    wanted = live["spec"].get("replicas", 1)
    if status.get("observedGeneration") != live["metadata"]["generation"]:
        return "the Deployment controller has not observed its newest generation"
    if status.get("updatedReplicas", 0) < wanted or status.get("unavailableReplicas"):
        return "a rollout is in flight"
    owner = live["metadata"].get("labels", {}).get(FLUX_NAME_LABEL)
    if not owner:
        return ""
    rc, out = flux("get", "kustomization", owner, "-o", "json")
    if rc != 0:
        return f"the Flux Kustomization {owner} could not be read"
    try:
        kustomization = json.loads(out)
    except ValueError:
        kustomization = None
    if not isinstance(kustomization, dict):
        return f"the Flux Kustomization {owner} could not be read"
    ready = parse_conditions(kustomization).get("Ready")
    if ready is not None and ready.status is ConditionStatus.TRUE:
        return ""
    return f"the Flux Kustomization {owner} is not Ready"


def restart(
    name: str, cfg: dict, now: float, runner: Callable[[str, int], Kubectl]
) -> Result:
    """Decide on `name` and, when nothing refuses it, restart its Deployment.

    Args:
        name: the unit instance, already matched against NAME.
        cfg: the parsed config file.
        now: the request's time, as epoch seconds.
        runner: `(namespace, timeout_s)` to a kubectl bound to them.
    """
    state_dir = Path(cfg["state_dir"])
    if Path(cfg["hold_file"]).exists():
        return Result("refused", "hold", f"{cfg['hold_file']} exists")
    entry = cfg["allowlist"].get(name)
    if entry is None:
        return Result("refused", "not-listed", f"{name} is not on the allowlist")
    if in_weekly_window(now, **cfg["reboot_mute"]):
        return Result("refused", "muted", "the weekly reboot's mute is open")
    if why := rate_refusal(name, cfg, past_restarts(state_dir), now):
        return Result("refused", "rate", why)

    deployment = entry["deployment"]
    read = runner(entry["namespace"], READ_TIMEOUT_S)
    before = read_deployment(read, deployment)
    if before is None:
        return Result("failed", "unreadable", f"could not read deployment/{deployment}")
    if why := busy_reason(before, runner(FLUX_NAMESPACE, READ_TIMEOUT_S)):
        return Result("refused", "busy", why)

    with (state_dir / "log.jsonl").open("a") as f:
        f.write(json.dumps({"ts": now, "name": name, "at": _utc(now)}) + "\n")
    rc, _ = read(
        "rollout",
        "restart",
        f"deployment/{deployment}",
        f"--field-manager={FIELD_MANAGER}",
    )
    if rc != 0:
        return Result("failed", "kubectl", f"kubectl rollout restart exited {rc}")
    timeout = cfg["rollout_timeout_s"]
    rolled, _ = runner(entry["namespace"], timeout + READ_TIMEOUT_S)(
        "rollout", "status", f"deployment/{deployment}", f"--timeout={timeout}s"
    )
    after = read_deployment(read, deployment)
    if after is None:
        return Result("failed", "unreadable", f"could not read deployment/{deployment}")
    if after["metadata"]["generation"] == before["metadata"]["generation"]:
        return Result(
            "failed", "no-write", "the Deployment's generation did not change"
        )
    if rolled != 0:
        return Result(
            "failed", "rollout", f"the rollout did not finish in {timeout} seconds"
        )
    return Result("restarted", detail=f"deployment/{deployment} rolled")


def post_to_discord(content: str, cfg: dict) -> None:
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    try:
        webhook = Path(directory, "discord_webhook").read_text().strip()
    except OSError:
        webhook = ""
    discord_post(
        webhook,
        content,
        TAG,
        log=lambda reason: log(reason, ERR, tag=TAG),
        marker=f"{TAG}:",
        spool_dir=str(Path(cfg["state_dir"]) / "discord-spool"),
    )


def main(
    argv: list[str],
    config_path: str = CONFIG,
    now: Callable[[], float] = time.time,
    runner: Callable[[str, int], Kubectl] | None = None,
    post: Callable[[str, dict], None] = post_to_discord,
) -> int:
    """Handle one request; `now`, `runner` and `post` are the seams for the clock, kubectl and Discord."""
    parser = argparse.ArgumentParser(
        prog="claude_restart.py",
        description="Restart the Deployment the allowlist entry NAME names, or refuse.",
    )
    parser.add_argument("name", help="a key of the allowlist in " + CONFIG)
    name = parser.parse_args(argv[1:]).name
    if not NAME.match(name):
        log("refusing a name outside the unit's shape", ERR, tag=TAG)
        return 2
    cfg = json.loads(Path(config_path).read_text())
    if runner is None:
        runner = lambda namespace, timeout: kubectl_runner(
            "kubectl", namespace, timeout
        )
    result = restart(name, cfg, now(), runner)
    line = "RESTART-RESULT: " + logfmt(
        {
            "name": name,
            "verdict": result.verdict,
            "cause": result.cause,
            "detail": result.detail,
        }
    )
    atomic_write(str(Path(cfg["state_dir"]) / "verdict"), line + "\n")
    log(line, ERR if result.verdict == "failed" else None, tag=TAG)
    if result.verdict != "refused":
        host = os.uname().nodename
        post(f"{name} on {host}: {result.verdict}. {result.detail}", cfg)
    return 1 if result.verdict == "failed" else 0


if __name__ == "__main__":
    install_excepthook()
    sys.exit(main(sys.argv))
