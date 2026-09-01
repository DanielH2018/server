#!/usr/bin/env python3
"""Post-deploy notifier for `scripts/deploy.sh --detach`.

Runs after the backgrounded ansible-playbook invocation finishes: gates the "settled"
verdict on `probe.py health <svc>` for every service tag that was deployed, then posts one
Discord message reusing gitops-deploy's own webhook and its `discord_post` helper — the same
channel every other automated deploy outcome in this repo already posts to (deploy failures,
CI gate rejections, rollbacks). An operator watching that channel is the one this notifier is
for; it does not invent a second alert path.

Degrades to a log-only note, never a crash, when:
  - a tag isn't a k8s Deployment/DaemonSet OR a Pi Docker container (a block tag like
    `config`/`deploy`/`cron`, or a name that matches neither) -- skipped from the verdict
    rather than counted as a failure
  - the gitops-deploy webhook config isn't present on this host -- deploy.sh normally runs on
    daniel-box, where it is; anywhere else this just prints and returns

Run: uv run pytest scripts/deploy_tools/test_deploy_detach_notify.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOST_LIB_PATH = Path("/opt/gitops-deploy/host_lib.py")
CONFIG_ENV_PATH = Path("/etc/gitops-deploy/config.env")
PROBE_TIMEOUT_S = 30

# Emitted by probe.py's health command when a tag names nothing health-checkable -- a block tag
# like config/deploy/cron, a typo --skip-tag-check let through, or a role (netpol-baseline,
# media-volume) whose manifests declare no workload at all.
#
# THESE MUST STAY UNAMBIGUOUS. "not found (not created" used to be on this list, and probe.py
# emitted it for BOTH "this tag names no container" and "the container that should exist is
# absent" -- so a failed deploy reported `skipped` and the verdict stayed `settled`. That is
# how PR #685's claude-otel health gate never ran while land.sh printed VERDICT: settled.
# probe.py now says which of the two happened, and its docstring records the rule: a name
# GUESSED from the tag may skip when absent, a name RESOLVED from the role's own manifests may
# not. `test_deploy_detach_notify.py` asserts every message probe.py emits for an absent
# workload matches none of these, so a rewording cannot quietly reopen the hole.
NOT_APPLICABLE_MARKERS = (
    "no Deployment or DaemonSet",
    "declares no rollout-checkable workload",
    "not a declared service on any host",
)


def check_one(tag: str, run=subprocess.run) -> tuple[str, str]:
    """('ok'|'unhealthy'|'skipped', first line of probe.py's output) for one service tag.

    Tries the k8s route first (~50 of ~55 services live there since the 2026-08-14 Docker
    retirement -- the same order probe.py's own run_health uses), falls back to --docker, and
    reports 'skipped' only when BOTH say the tag isn't that kind of workload.
    """

    def probe(extra: list[str]) -> tuple[int, str]:
        try:
            res = run(
                [
                    "uv",
                    "run",
                    "python",
                    "scripts/diagnostics/probe.py",
                    "health",
                    tag,
                    *extra,
                ],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return 1, f"{tag}: probe.py health timed out after {PROBE_TIMEOUT_S}s"
        out = (res.stdout or res.stderr or "").strip()
        return res.returncode, out.splitlines()[0] if out else ""

    code, line = probe([])
    if code == 0:
        return "ok", line
    if any(marker in line for marker in NOT_APPLICABLE_MARKERS):
        code, line = probe(["--docker"])
        if code == 0:
            return "ok", line
        if any(marker in line for marker in NOT_APPLICABLE_MARKERS):
            return "skipped", line
    return "unhealthy", line


def gate(
    tags: list[str], ansible_ok: bool, run=subprocess.run
) -> tuple[bool, list[str]]:
    """(settled, report_lines). `settled` is the notification's headline verdict.

    A failed ansible-playbook run is authoritative on its own -- no health check runs, since a
    failed apply didn't necessarily reach the point of rolling anything out.
    """
    if not ansible_ok:
        return False, ["ansible-playbook exited non-zero -- see the log."]
    lines = []
    ok = True
    if not tags:
        lines.append("no --tags given -- health not gated, ansible exit code only.")
    for tag in tags:
        state, detail = check_one(tag, run=run)
        if state == "skipped":
            lines.append(
                f"{tag}: not a health-checkable workload (skipped) -- {detail}"
            )
            continue
        lines.append(detail or f"{tag}: {state}")
        if state == "unhealthy":
            ok = False
    return ok, lines


def notify(content: str) -> None:
    """Best-effort Discord post reusing gitops-deploy's webhook + helper. Never raises."""
    if not HOST_LIB_PATH.exists() or not CONFIG_ENV_PATH.exists():
        print(
            "deploy --detach: no gitops-deploy webhook config on this host "
            f"({HOST_LIB_PATH} / {CONFIG_ENV_PATH} not found) -- skipping Discord notify."
        )
        return
    sys.path.insert(0, str(HOST_LIB_PATH.parent))
    try:
        from host_lib import discord_post, parse_env_file  # type: ignore

        cfg = parse_env_file(str(CONFIG_ENV_PATH))
        webhook = cfg.get("DISCORD_WEBHOOK", "")
        posted = discord_post(
            webhook, content, "deploy-detach", marker="deploy --detach:"
        )
        if not posted:
            print(
                "deploy --detach: Discord post failed or webhook unset -- see log above."
            )
    except Exception as exc:  # notifying must never crash the backgrounded run
        print(
            f"deploy --detach: notify failed ({exc}) -- deploy result is only in this log."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--status", type=int, required=True, help="ansible-playbook's exit status"
    )
    parser.add_argument(
        "--log", required=True, help="path to the deploy's captured output"
    )
    parser.add_argument(
        "--tags", default="", help="comma-separated service tags that were deployed"
    )
    parser.add_argument(
        "--no-post",
        action="store_true",
        help=(
            "print the verdict, do not post it. land.sh returns the verdict to the "
            "session instead; posting from both paths would split one verdict across "
            "two channels."
        ),
    )
    ns = parser.parse_args(argv)

    tags = [t for t in ns.tags.split(",") if t]
    settled, lines = gate(tags, ns.status == 0)

    headline = "settled" if settled else "FAILED"
    content = "\n".join(
        [
            f"deploy --detach {headline} (ansible exit {ns.status})",
            *lines,
            f"log: {ns.log}",
        ]
    )
    print(content)
    if not ns.no_post:
        notify(content)
    return 0 if settled else 1


if __name__ == "__main__":
    sys.exit(main())
