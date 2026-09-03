#!/usr/bin/env python3
"""Generates docs/reference/state.md, "State of the lab" -- one row per autonomous loop.

This is the page a session or a review reads first: not what is deployed, but whether the
crons and timers that keep the homelab honest are actually still ticking. Every other
generated page describes CONFIGURATION (what is installed, what is scheduled to run); this
one describes OUTCOME (what last happened, and whether that is recent enough to trust).

SOURCES. Each loop leaves state on the host it runs on: a `last_run` marker, a drill's
success stamp, or (for the two crons with no local file) the last commit that moved their
tracked source. The generator runs on daniel-box under the docs-refresh cron as the `ubuntu`
user, so every reader degrades: a missing state directory means "not reachable from here"
(wrong host, no permission, role not installed) rather than a crash, and the whole page is
never failed by one unreadable loop -- see WHAT UNREADABLE MEANS below.

WHAT UNREADABLE MEANS, vs NEVER. The state directory not existing is "unreadable": this
checkout cannot see that loop's evidence at all. The directory existing but the specific
marker file missing is "never": the loop's plumbing is here, it has just not completed a
run yet. That split is what lets a fresh install (dir created, no run yet) read differently
from a generator running off-host (no dir at all) -- both would otherwise look identical.

STATUS is the same rule for every loop: late when age > 2x cadence. `ok`, `late`, `never`
and `unreadable` are exhaustive; there is no "unknown cadence" status -- a loop whose cadence
this generator cannot derive reads `ok` as long as it has ever run, because "late relative to
an interval we cannot state" is not a claim this page makes.

CADENCE comes from crons.cadence_minutes() wherever the loop is an `ansible.builtin.cron`
task (matched by job name against crons.build_rows()) or its own systemd timer interval
(gitops-deploy, renovate-agent, renovate-notify -- none of which are cron jobs, so they
carry a hardcoded value read once from the role that defines the timer, cited in a comment
at each constant). Two crons -- the Longhorn and etcd restore drills -- build their schedule
with `.split()[N]` off one compound var, so crons.py's own schedule column prints unresolved
Jinja on 3-5 of the five fields and the structural read above cannot tell a genuinely
restricted field from a templated one that happens to resolve to `*`. Reading that one
compound default directly and feeding the REAL resolved string to cadence_minutes() is not a
second parser -- it is the one parser, given honest input for the one shape it cannot read
through Jinja.

Usage::

    uv run python scripts/docs/reference/state.py --out docs/reference/state.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import yaml

from docs.reference import crons as crons_mod
from lib.git import git
from lib.repo_paths import REPO, ROLES

LATE_MULTIPLIER = 2

_K3S_DEFAULTS = ROLES / "setup" / "k3s" / "defaults" / "main.yml"
_DOCS_SITE_BUILD_INFO = Path.home() / "docs-site" / "build-info.json"


@dataclass(frozen=True)
class LoopRun:
    """One loop's last-known state.

    Attributes:
        last_run: When the loop last completed, or None if that cannot be established.
        outcome: A one-line, human-readable summary of what happened -- always set, even
            when last_run is None, so a reader knows WHY there is nothing to show.
        unreadable: True when the source could not be read at all (wrong host, permission,
            unparseable content) -- distinct from `last_run is None` with unreadable False,
            which means the source was read fine and simply has no run recorded yet.
    """

    last_run: dt.datetime | None
    outcome: str
    unreadable: bool = False


def _epoch_marker(state_dir: Path, filename: str, outcome_ok: str) -> LoopRun:
    """Read `<state_dir>/<filename>` as a bare unix-epoch marker.

    Shared by every loop whose state file is nothing but `str(time.time())` or
    `date +%s` -- gitops-deploy, renovate-agent, renovate-notify, and the Longhorn restore
    drill all write exactly that.
    """
    if not state_dir.is_dir():
        return LoopRun(None, "state directory not reachable from here", unreadable=True)
    path = state_dir / filename
    if not path.is_file():
        return LoopRun(None, "no run recorded yet")
    try:
        epoch = float(path.read_text().strip())
    except OSError, ValueError:
        return LoopRun(None, "state file content did not parse", unreadable=True)
    return LoopRun(dt.datetime.fromtimestamp(epoch, dt.timezone.utc), outcome_ok)


def gitops_deploy_run(state_dir: Path = Path("/var/lib/gitops-deploy")) -> LoopRun:
    """The 10-minute GitOps deploy tick.

    Cadence: OnUnitActiveSec in gitops-deploy.timer.j2, from gitops_deploy_tick_interval
    (roles/setup/gitops_deploy/defaults/main.yml:37 = 10min).
    """
    base = _epoch_marker(state_dir, "last_run", "ticked, no hold")
    if base.last_run is None:
        return base
    hold_sha_path = state_dir / "hold_sha"
    if not hold_sha_path.is_file():
        return base
    sha = hold_sha_path.read_text().strip()[:8] or "unknown"
    plane_path = state_dir / "hold_plane"
    plane = (
        plane_path.read_text().strip() if plane_path.is_file() else "a service deploy"
    )
    return LoopRun(
        base.last_run,
        f"**HOLD** at `{sha}` ({plane}) -- a health gate or broad apply failed and is "
        "parked until cleared",
    )


def renovate_agent_run(state_dir: Path = Path("/var/lib/renovate-agent")) -> LoopRun:
    """The daily Renovate PR triage session.

    Cadence: renovate_agent.timer's OnCalendar, from renovate_agent_oncalendar
    (roles/setup/renovate_agent/defaults/main.yml:47 = daily 15:00).
    """
    return _epoch_marker(state_dir, "last_run", "session completed")


def renovate_notify_run(state_dir: Path = Path("/var/lib/renovate-notify")) -> LoopRun:
    """The daily Renovate dashboard digest.

    Cadence: renovate-notify.timer's OnCalendar is fixed at daily 13:00
    (roles/setup/renovate_notify/templates/renovate-notify.timer.j2).
    """
    base = _epoch_marker(state_dir, "last_run", "checked, nothing new to notify")
    if base.last_run is None:
        return base
    fingerprint_path = state_dir / "last_notified"
    if fingerprint_path.is_file() and fingerprint_path.read_text().strip():
        return LoopRun(base.last_run, "notified")
    return base


def docs_refresh_run(build_info: Path = _DOCS_SITE_BUILD_INFO) -> LoopRun:
    """The twice-daily docs-refresh cron.

    Cadence: crons.build_rows() job "Refresh generated docs". Reads build-info.json, the
    served-site stamp build_docs.py writes -- see
    ansible/roles/setup/initial_setup/templates/docs-refresh.sh.j2.
    """
    if not build_info.is_file():
        return LoopRun(None, "build-info.json not reachable from here", unreadable=True)
    try:
        data = json.loads(build_info.read_text())
        built_at = dt.datetime.strptime(data["built_at"], "%Y-%m-%d %H:%M UTC").replace(
            tzinfo=dt.timezone.utc
        )
        generators = data.get("generators", "unknown")
    except OSError, json.JSONDecodeError, KeyError, ValueError:
        return LoopRun(None, "build-info.json content did not parse", unreadable=True)
    return LoopRun(built_at, f"generators: {generators}")


def secret_rotate_run(repo: Path = REPO) -> LoopRun:
    """The weekly secret-rotation cron.

    Cadence: crons.build_rows() job "Weekly secret rotation (auto tier)".

    No local state file -- this is the last commit that touched the plaintext rotation
    registry, which also moves on a manual `secret_rotation.py rotate`, not only the cron. A
    proxy, not the cron's own liveness signal; the docstring on the task names this trade.
    """
    target = repo / "ansible" / "secret_rotation.yml"
    if not target.is_file():
        return LoopRun(
            None,
            "ansible/secret_rotation.yml not found in this checkout",
            unreadable=True,
        )
    result = git(
        "log",
        "-1",
        "--format=%cI%n%s",
        "--",
        "ansible/secret_rotation.yml",
        cwd=repo,
        check=False,
        timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return LoopRun(
            None, "no commit history for the rotation registry", unreadable=True
        )
    when_line, _, subject = result.stdout.strip().partition("\n")
    try:
        when = dt.datetime.fromisoformat(when_line)
    except ValueError:
        return LoopRun(None, "commit date did not parse", unreadable=True)
    return LoopRun(when, f"last touched by: {subject or '(no subject)'}")


def longhorn_restore_drill_run(
    state_dir: Path = Path("/var/lib/longhorn-restore-drill"),
) -> LoopRun:
    """The daily Longhorn PVC restore drill.

    Cadence: k3s_longhorn_restore_drill_cron (roles/setup/k3s/defaults/main.yml, resolved
    directly -- see the module docstring).
    """
    return _epoch_marker(state_dir, "last-success", "PVC restore proven")


def etcd_restore_drill_run(
    state_dir: Path = Path("/var/lib/etcd-restore-drill"),
) -> LoopRun:
    """The weekly etcd list-only restore drill.

    Cadence: k3s_etcd_restore_drill_cron (roles/setup/k3s/defaults/main.yml, resolved
    directly -- see the module docstring).
    """
    if not state_dir.is_dir():
        return LoopRun(None, "state directory not reachable from here", unreadable=True)
    path = state_dir / "last-success-list-only"
    if not path.is_file():
        return LoopRun(None, "no run recorded yet")
    try:
        fields = dict(
            line.split("=", 1) for line in path.read_text().splitlines() if "=" in line
        )
        when = dt.datetime.fromtimestamp(float(fields["epoch"]), dt.timezone.utc)
    except OSError, ValueError, KeyError:
        return LoopRun(None, "state file content did not parse", unreadable=True)
    snapshot = fields.get("snapshot", "unknown")
    return LoopRun(when, f"list-only restore proven (snapshot {snapshot})")


def _k3s_default_var(name: str, defaults_path: Path = _K3S_DEFAULTS) -> str | None:
    """A single scalar default from roles/setup/k3s/defaults/main.yml."""
    try:
        data = yaml.safe_load(defaults_path.read_text())
    except OSError, yaml.YAMLError:
        return None
    value = (data or {}).get(name)
    return str(value) if value is not None else None


def _cron_job_cadence(job_name: str, roles: Path = ROLES) -> float | None:
    """cadence_minutes() applied to a cron job's own schedule, found by its `name:`."""
    rows = {row["name"]: row for row in crons_mod.build_rows(roles)}
    row = rows.get(job_name)
    if row is None:
        return None
    return crons_mod.cadence_minutes(row["schedule"])


# name, reader, cadence (minutes) -- built once at call time so tests can import the
# individual reader functions above without paying for a live git/yaml read at import time.
def _loops(roles: Path = ROLES) -> list[tuple[str, LoopRun, float | None]]:
    longhorn_default = _k3s_default_var("k3s_longhorn_restore_drill_cron")
    etcd_default = _k3s_default_var("k3s_etcd_restore_drill_cron")
    return [
        ("gitops-deploy", gitops_deploy_run(), 10.0),
        ("renovate-agent", renovate_agent_run(), 1440.0),
        ("renovate-notify", renovate_notify_run(), 1440.0),
        (
            "docs-refresh",
            docs_refresh_run(),
            _cron_job_cadence("Refresh generated docs", roles),
        ),
        (
            "secret-rotate",
            secret_rotate_run(),
            _cron_job_cadence("Weekly secret rotation (auto tier)", roles),
        ),
        (
            "longhorn-restore-drill",
            longhorn_restore_drill_run(),
            crons_mod.cadence_minutes(longhorn_default) if longhorn_default else None,
        ),
        (
            "etcd-restore-drill",
            etcd_restore_drill_run(),
            crons_mod.cadence_minutes(etcd_default) if etcd_default else None,
        ),
    ]


def status_for(run: LoopRun, cadence: float | None, now: dt.datetime) -> str:
    """ok / late / never / unreadable -- the one rule every loop is judged by."""
    if run.unreadable:
        return "unreadable"
    if run.last_run is None:
        return "never"
    if cadence is None:
        return "ok"
    age_minutes = (now - run.last_run).total_seconds() / 60
    return "late" if age_minutes > LATE_MULTIPLIER * cadence else "ok"


def _format_duration(minutes: float | None) -> str:
    if minutes is None:
        return "unknown"
    total = round(minutes)
    if total < 60:
        return f"{total}m"
    hours, rem_m = divmod(total, 60)
    if hours < 24:
        return f"{hours}h{rem_m}m" if rem_m else f"{hours}h"
    days, rem_h = divmod(hours, 24)
    return f"{days}d{rem_h}h" if rem_h else f"{days}d"


def build_rows(
    roles: Path = ROLES, now: dt.datetime | None = None
) -> list[dict[str, str]]:
    """One row per autonomous loop, statuses computed against `now` (default: real time).

    Args:
        roles: Root directory `_cron_job_cadence` searches for the loops backed by an
            `ansible.builtin.cron` task.
        now: The instant to compute age/status against. Defaults to the real current time;
            tests pass a fixed instant so a fixture's freshness does not rot.

    Returns:
        One dict per loop, keyed by the columns `render_markdown` renders.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = []
    for name, run, cadence in _loops(roles):
        last_run_local = run.last_run.astimezone() if run.last_run else None
        age = (
            _format_duration((now - run.last_run).total_seconds() / 60)
            if run.last_run
            else "—"
        )
        rows.append(
            {
                "name": name,
                "last_run": last_run_local.strftime("%Y-%m-%dT%H:%M:%S%z")
                if last_run_local
                else "never",
                "age": age,
                "cadence": _format_duration(cadence),
                "status": status_for(run, cadence, now),
                "outcome": run.outcome,
            }
        )
    return rows


def render_markdown(rows: list[dict[str, str]]) -> str:
    """Renders the "State of the lab" page: banner, one-line summary, then the table.

    Args:
        rows: Loop rows as returned by `build_rows`.

    Returns:
        The full page as Markdown text, ending in a single trailing newline.
    """
    from lib.docs_provenance import generated_banner

    ok_count = sum(1 for r in rows if r["status"] == "ok")
    parts = [generated_banner("scripts/docs/reference/state.py")]
    parts.append("# State of the lab\n")
    parts.append(f"{ok_count} of {len(rows)} loops within cadence.\n")
    parts.append(
        '!!! warning "Status is a heuristic over the last recorded state"\n'
        "    `late` means the loop's last recorded run is more than 2x its expected "
        "cadence old. `unreadable` means this generator could not reach the loop's state "
        "at all (wrong host, permission, or unparseable content) -- not that the loop is "
        "unhealthy. `never` means the state is reachable and simply has no run recorded "
        "yet.\n"
    )
    parts.append("| Loop | Last run | Age | Cadence | Status | Last outcome |")
    parts.append("|---|---|---|---|---|---|")
    for row in rows:
        from lib.docs_provenance import md_cell

        parts.append(
            f"| {row['name']} | {row['last_run']} | {row['age']} | {row['cadence']} | "
            f"{row['status']} | {md_cell(row['outcome'])} |"
        )
    return "\n".join(parts).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    """Build the loop rows, render the reference page, and write it if the body changed.

    Returns:
        The exit code from `finish_generator` (0 on success, non-zero on a write failure).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    parser.add_argument("--roles", type=Path, default=ROLES)
    args = parser.parse_args(argv)

    from lib.docs_provenance import finish_generator

    rows = build_rows(args.roles)
    return finish_generator(
        "docs.reference.state", args.out, rows, render_markdown, "loop"
    )


if __name__ == "__main__":
    raise SystemExit(main())
