#!/usr/bin/env python3
"""Post-deploy notifier for `scripts/deploy.sh --detach`.

Runs after the backgrounded ansible-playbook invocation finishes: gates the "settled"
verdict on `probe.py health <svc>` for every service tag that was deployed, then posts one
Discord message reusing gitops-deploy's own webhook and its `discord_post` helper — the same
channel every other automated deploy outcome in this repo already posts to (deploy failures,
CI gate rejections, rollbacks). An operator watching that channel is the one this notifier is
for; it does not invent a second alert path.

Degrades to a log-only note, never a crash, when:
  - a tag isn't a k8s workload OR a Pi Docker container (a block tag like
    `config`/`deploy`/`cron`, a name that matches neither, or a role whose manifests declare no
    workload at all -- netpol-baseline, media-volume) -- skipped from the verdict rather than
    counted as a failure
  - the gitops-deploy webhook config isn't present on this host -- deploy.sh normally runs on
    daniel-box, where it is; anywhere else this just prints and returns

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_detach_notify.py
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import REPO

# Same directory, so a direct invocation already has it on sys.path. `tag_platforms` is the
# reader of containers_list that says which probe can see a tag's workload.
import deploy_tags

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


def check_one(
    tag: str, run=subprocess.run, platforms: set[str] | None = None
) -> tuple[str, str]:
    """('ok'|'unhealthy'|'skipped', first line of probe.py's output) for one service tag.

    `platforms` is the set of platforms whose containers_list declares the tag, read from the
    inventory when not given. It decides which probe may answer:

      - {'docker'} probes the Pi only, and a miss there is `unhealthy`, never `skipped`. The
        k8s probe is not consulted at all: a Docker-only tag has no role under roles/k8s/, so
        probe.py falls back to guessing a workload by the tag's name, and a same-named cluster
        workload answers for it. That is issue #929: `alloy` is the Pi's log shipper AND the
        name of loki-homelab's cluster DaemonSet, so the gate read the DaemonSet's 2/2 ready
        and reported the Pi's undeployed container healthy.
      - {'k8s'} probes the cluster only, `skipped` when the role declares no workload.
      - both probes both, and each side's own rule applies.
      - neither (a block tag, a typo --skip-tag-check let through) keeps the old order: k8s
        first, --docker second, `skipped` only when BOTH say the tag isn't that kind of
        workload.
    """

    def probe(extra: list[str]) -> tuple[int, str]:
        """Run probe.py health for `tag` with `extra` args; return (exit code, first line)."""
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

    def not_applicable(line: str) -> bool:
        return any(marker in line for marker in NOT_APPLICABLE_MARKERS)

    if platforms is None:
        platforms = deploy_tags.tag_platforms(tag)

    if platforms == {"docker"}:
        code, line = probe(["--docker"])
        return ("ok" if code == 0 else "unhealthy"), line

    if platforms == {"k8s"}:
        code, line = probe([])
        if code == 0:
            return "ok", line
        return ("skipped" if not_applicable(line) else "unhealthy"), line

    if platforms == {"docker", "k8s"}:
        k8s_code, k8s_line = probe([])
        docker_code, docker_line = probe(["--docker"])
        line = f"{k8s_line}; {docker_line}"
        if docker_code != 0 or (k8s_code != 0 and not not_applicable(k8s_line)):
            return "unhealthy", line
        return "ok", line

    code, line = probe([])
    if code == 0:
        return "ok", line
    if not_applicable(line):
        code, line = probe(["--docker"])
        if code == 0:
            return "ok", line
        if not_applicable(line):
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
    # Removed again in the `finally`: the entry outlives the call otherwise, and a later
    # bare-name `import host_lib` anywhere in the process then resolves against whatever
    # sits in that directory. Under pytest that directory is a tmp_path holding a
    # deliberately broken stub, which failed an unrelated test in another module.
    lib_dir = str(HOST_LIB_PATH.parent)
    sys.path.insert(0, lib_dir)
    try:
        from host_lib import discord_post, parse_env_file  # type: ignore[import-not-found]

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
    finally:
        # `remove` takes the first occurrence, which is the one inserted above.
        with contextlib.suppress(ValueError):
            sys.path.remove(lib_dir)


def main(argv: list[str] | None = None) -> int:
    """Gate the deploy's health, post the verdict to Discord, and exit 0 if settled else 1."""
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
