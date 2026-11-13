"""The health gate a landing ends with: `probe.py health` for every tag a promotion applied.

Each tag's state is `ok`, `unhealthy`, `unreadable` (the probe could not read its store) or
`skipped` (the tag declares no workload), and `gate` folds them into one verdict.

Run: uv run pytest scripts/deploy_tools/tests/test_land_health_gate.py
"""

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/

from lib.repo_paths import HOST_VARS, REPO
from lib.render_guard import entry_tags, k8s_services, services_file
from lib.summary import grouped
from diagnostics.probe_lib.health_checkout import STALE_CHECKOUT
from lib.exit_codes import PROBE_NOT_APPLICABLE, PROBE_UNREADABLE

# The inventory directory as a path relative to a checkout root. `check_one` reads the
# inventory from whichever tree the probe renders the manifests from, which is not always this
# one, so it needs the shape of that path without repo_paths' own root baked in.
HOST_VARS_REL = HOST_VARS.relative_to(REPO)


def tag_platforms(tag: str, host_vars: Path = HOST_VARS) -> set[str]:
    """`{"k8s"}` when a k8s service in the tree whose host_vars are `host_vars` carries `tag`.

    Empty for a tag no service declares. A Docker-only tag has nothing on the cluster to check,
    and probing it there first could find a same-named cluster workload (loki-homelab's Alloy
    DaemonSet) and report the Pi's undeployed container healthy.
    """
    services = k8s_services(services_file(host_vars))
    return {"k8s"} if any(tag in entry_tags(e) for e in services) else set()


# One `probe.py health` run. The alert plane sets the floor: up to 90s waiting for a probe
# sample newer than the rollout, a 60s re-read of a would-fail verdict, and a second probe
# wait on that re-read (`probe_lib/health_alerts.py`), on top of the render and kubectl reads.
PROBE_TIMEOUT_S = 300


@dataclass(frozen=True)
class GateTools:
    """The two boundaries the health gate crosses, so a test replaces a field, not a module.

    `run` is the probe subprocess; `tag_platforms` reads the inventory. Both default to the
    real implementation, the way `land_lib.tools.Tools` does.
    """

    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    # Takes the inventory to read as a keyword, because which TREE answers is decided per call
    # (see `check_one`), not once when the tools are built.
    tag_platforms: Callable[..., set[str]] = field(
        default=lambda tag, host_vars: tag_platforms(tag, host_vars)
    )


class CheckResult(NamedTuple):
    """One tag's health: the state the verdict counts, and the line an operator reads."""

    state: str
    detail: str


# How the gate's count line names each `check_one` state.
COUNTED_AS = {"ok": "healthy", "unreadable": "not graded"}


class GateResult(NamedTuple):
    """The landing's headline verdict, a count line, and a line per tag not healthy.

    A healthy or skipped tag is only counted, so a broad landing's log stays short (#7894).

    `unreadable` is True when the gate is unsettled only because a probe could not read its
    store (`PROBE_UNREADABLE`): no tag was found unhealthy, and at least one was not graded at
    all. Prometheus rolling under the gate is the usual cause (#5543), and a re-run a minute
    later answers. An unhealthy tag beside an unreadable one leaves it False, because one
    workload already failed.

    `stale` is True when the probe refused at least one ungraded tag because the tree it ran
    in holds an older role definition than `origin/master` (`health_checkout.STALE_CHECKOUT`).
    A re-run from that same tree refuses again, so it is no store that will answer shortly.
    `unreadable` is True with it.
    """

    settled: bool
    lines: list[str]
    unreadable: bool = False
    stale: bool = False


def check_one(
    tag: str,
    tools: GateTools | None = None,
    platforms: set[str] | None = None,
    cwd: Path | None = None,
) -> CheckResult:
    """('ok'|'unhealthy'|'unreadable'|'skipped', first line of probe.py's output) for one tag.

    `unreadable` is probe.py's `PROBE_UNREADABLE`: it could not reach the store it reads, so
    the workload was not graded. That is no verdict on the workload, healthy or not (#5543).

    `cwd` is the checkout the probe runs in, defaulting to this file's own. It decides WHICH
    TREE THE ROLE'S MANIFESTS ARE RENDERED FROM, because `scripts/diagnostics/probe.py` is
    named relatively here and `probe_lib.health` derives every path it reads from the probe.py
    it resolved: a role whose workloads exist only in a commit this checkout has not pulled
    enumerates nothing and reads as `skipped`. A landing that deployed a snapshot of its merge
    commit passes that snapshot, so the workload list comes from the tree that was deployed.

    `platforms` is the set of platforms whose containers_list declares the tag, read when not
    given from the inventory of that SAME tree -- both halves of one verdict come from the tree
    that was deployed. It decides which probe may answer:

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

    tools = tools or GateTools()

    def probe(extra: list[str]) -> tuple[int, str]:
        """Run probe.py health for `tag` with `extra` args; return (exit code, first line)."""
        try:
            res = tools.run(
                [
                    "uv",
                    "run",
                    "python",
                    "scripts/diagnostics/probe.py",
                    "health",
                    tag,
                    *extra,
                ],
                cwd=cwd or REPO,
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return 1, f"{tag}: probe.py health timed out after {PROBE_TIMEOUT_S}s"
        if res.returncode == PROBE_UNREADABLE and (res.stderr or "").strip():
            # stdout holds only the PROBE-VERDICT line here, and the failed read is on stderr.
            # Its LAST line, because `uv run` can print its own warnings ahead of it.
            return res.returncode, res.stderr.strip().splitlines()[-1]
        out = (res.stdout or res.stderr or "").strip()
        return res.returncode, out.splitlines()[0] if out else ""

    def graded(code: int) -> str:
        if code == 0:
            return "ok"
        return "unreadable" if code == PROBE_UNREADABLE else "unhealthy"

    if platforms is None:
        # The SAME tree the probe renders from, not this checkout's inventory: a PR that adds a
        # Pi role and its containers_list entry together declares the tag only at the commit
        # that was deployed, and the primary reads `set()` for it -- which falls through to the
        # last branch below and probes k8s FIRST, where a same-named cluster workload answers.
        platforms = tools.tag_platforms(tag, host_vars=(cwd or REPO) / HOST_VARS_REL)

    if platforms == {"docker"}:
        code, line = probe(["--docker"])
        return CheckResult(graded(code), line)

    if platforms == {"k8s"}:
        code, line = probe([])
        if code == PROBE_NOT_APPLICABLE:
            return CheckResult("skipped", line)
        return CheckResult(graded(code), line)

    if platforms == {"docker", "k8s"}:
        k8s_code, k8s_line = probe([])
        docker_code, docker_line = probe(["--docker"])
        line = f"{k8s_line}; {docker_line}"
        states = {graded(docker_code)}
        if k8s_code != PROBE_NOT_APPLICABLE:
            states.add(graded(k8s_code))
        # One side found unhealthy outranks the other side being unreadable.
        for state in ("unhealthy", "unreadable"):
            if state in states:
                return CheckResult(state, line)
        return CheckResult("ok", line)

    code, line = probe([])
    if code == 0:
        return CheckResult("ok", line)
    if code == PROBE_NOT_APPLICABLE:
        code, line = probe(["--docker"])
        if code == 0:
            return CheckResult("ok", line)
        if code == PROBE_NOT_APPLICABLE:
            return CheckResult("skipped", line)
    return CheckResult(graded(code), line)


def gate(
    tags: list[str],
    tools: GateTools | None = None,
    cwd: Path | None = None,
) -> GateResult:
    """(settled, report_lines). `settled` is the landing's headline verdict.

    `cwd` is the checkout the probe renders the role's manifests from; `check_one` carries the
    argument and the reason. None renders the checkout this file lives in.
    """
    lines, counted = [], []
    unhealthy = unreadable = stale = False
    for tag in tags:
        state, detail = check_one(tag, tools=tools, cwd=cwd)
        counted.append((tag, COUNTED_AS.get(state, state)))
        if state in ("ok", "skipped"):
            continue
        if state == "unreadable" and STALE_CHECKOUT in detail:
            lines.append(
                f"{tag}: not graded -- the gate's tree is older than origin/master: {detail}"
            )
            unreadable = stale = True
            continue
        if state == "unreadable":
            lines.append(
                f"{tag}: not graded -- probe.py health could not read its store: {detail}"
            )
            unreadable = True
            continue
        lines.append(detail or f"{tag}: {state}")
        if state == "unhealthy":
            unhealthy = True
    if counted:
        lines.insert(0, f"health: {grouped(counted, named_up_to=0)}")
    return GateResult(
        not (unhealthy or unreadable),
        lines,
        unreadable=unreadable and not unhealthy,
        stale=stale and not unhealthy,
    )
