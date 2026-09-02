"""Where a `scripts/...` path can be executed from, read once for two different questions.

`scripts/docs/gen_reference_scripts.py` classifies HOW a script is run (scheduled, gated,
library, adhoc) so the generated reference page can say so. `scripts/test_invoker_paths_resolve.py`
asserts every `scripts/...` token an invoker names still resolves to a real file, so a rename
that misses one of these sites fails a test instead of a 3am cron. Both walk the same places —
`prek.toml`, the GitHub Actions workflows, an `ansible.builtin.cron` `job:`, the `initial_setup`
shell templates, `.claude/hooks` wrappers, `.claude/settings*.json` permissions — and until now
each carried its own copy of "which files, which field", which is exactly the kind of fact that
drifts when only one copy gets updated after a reorg.

WHAT LIVES HERE VS. WHAT DOESN'T. This module owns file discovery and field extraction — WHICH
files count as an invocation site, and WHICH field of each one is the text that actually runs.
It does not decide what counts as a reference inside that text; a caller regexes the returned
text for its own purpose (a bare `scripts/...` token for the guard test, a run-context-aware
scan that also tolerates comments and mentions for the generator's classifier). Every source
lives here, but not every source has two consumers: `prek_hook_entries` and `workflow_run_steps`
are read only by the guard test today. `gen_reference_scripts.py`'s classifier keeps its own
whole-file scan of `prek.toml` and each workflow instead of the field-scoped one, because it
deliberately reads more than the `entry=`/`run:` field alone — its own test
(`test_an_argv_element_in_python_source_is_an_invocation`) pins a prek.toml hook keyed
`other = "..."` as a valid invocation, which a strict `entry=`-only extraction would miss.
Field-scoping it to match the guard test would narrow the classifier's coverage.

Import it through the same bootstrap as any other cross-directory `lib` import (repo-root
CLAUDE.md, *Directory Structure*)::

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from lib.invocation_sites import cron_jobs  # noqa: E402
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.repo_paths import REPO


def _is_archived(path: Path) -> bool:
    return "/archive/" in path.as_posix()


# --- prek.toml --------------------------------------------------------------------------


def prek_hook_entries(repo: Path = REPO) -> list[tuple[str, str]]:
    """(location, entry text) for every `entry =` in a `prek.toml` hook."""
    path = repo / "prek.toml"
    if not path.is_file():
        return []
    config = tomllib.loads(path.read_text())
    out = []
    for hook_repo in config.get("repos", []):
        for hook in hook_repo.get("hooks", []):
            entry = hook.get("entry")
            if entry:
                out.append((f"prek.toml hook {hook.get('id')!r} entry=", entry))
    return out


# --- GitHub Actions workflows -------------------------------------------------------------


def workflow_files(repo: Path = REPO) -> list[Path]:
    return sorted((repo / ".github" / "workflows").glob("*.yml"))


def workflow_run_steps(repo: Path = REPO) -> list[tuple[str, str]]:
    """(location, run text) for every `run:` step in a workflow job."""
    out = []
    for wf in workflow_files(repo):
        data = yaml.safe_load(wf.read_text()) or {}
        for job_name, job in (data.get("jobs") or {}).items():
            for i, step in enumerate(job.get("steps") or []):
                run = step.get("run")
                if run:
                    loc = f"{wf.relative_to(repo)} job {job_name!r} step {i}"
                    out.append((loc, run))
    return out


# --- ansible.builtin.cron ------------------------------------------------------------------


@dataclass(frozen=True)
class CronJob:
    path: Path
    name: str
    job: str


def cron_jobs(repo: Path = REPO) -> list[CronJob]:
    """Every present `ansible.builtin.cron` task in the tree, wherever it lives.

    Tree-wide by design: cron tasks are not confined to `initial_setup` (traefik,
    claude-otel, qbittorrent and eight other roles each carry their own), so a reader
    scoped to one file misses the rest.
    """
    jobs = []
    for path in sorted((repo / "ansible").rglob("tasks/*.yml")):
        if _is_archived(path):
            continue
        try:
            loaded = yaml.safe_load(path.read_text())
        except OSError, yaml.YAMLError:
            continue
        if not isinstance(loaded, list):
            continue
        for task in loaded:
            if not isinstance(task, dict) or "ansible.builtin.cron" not in task:
                continue
            spec = task["ansible.builtin.cron"] or {}
            if str(spec.get("state", "present")) == "absent":
                continue
            job = str(spec.get("job", ""))
            if job:
                jobs.append(CronJob(path, str(spec.get("name", "unnamed")), job))
    return jobs


# --- shell-wrapper templates ---------------------------------------------------------------


def sh_j2_templates(repo: Path = REPO) -> list[Path]:
    """Every `*.sh.j2` template in the tree — a cron's `job:` can name one by basename,
    and `initial_setup`'s templates are themselves a stale-path invocation site."""
    return sorted(
        p for p in (repo / "ansible").rglob("templates/*.sh.j2") if not _is_archived(p)
    )


# --- .claude/hooks wrappers ------------------------------------------------------------------


def claude_hook_files(repo: Path = REPO) -> list[Path]:
    """Every non-test `.claude/hooks` wrapper — a script this repo runs on its own commands."""
    hooks_dir = repo / ".claude" / "hooks"
    files = sorted(hooks_dir.glob("*.sh")) + sorted(hooks_dir.glob("*.py"))
    return [f for f in files if f.is_file() and not f.name.startswith("test_")]


# --- systemd units -----------------------------------------------------------------------


def systemd_exec_lines(repo: Path = REPO) -> list[tuple[str, str]]:
    """(location, line text) for every `ExecStart*=` line in a systemd `*.service.j2`.

    No unit currently execs a `scripts/...` path directly (verified by hand 2026-08-27):
    every `ExecStart` that reaches this repo's tooling runs a copy already staged to
    `/opt`. Kept live rather than removed so the day a unit DOES call into `scripts/`
    directly, this catches it instead of staying silently zero forever.
    """
    out = []
    for svc in sorted(repo.glob("ansible/roles/**/*.service.j2")):
        for lineno, line in enumerate(svc.read_text().splitlines(), start=1):
            if line.strip().startswith("ExecStart"):
                out.append((f"{svc.relative_to(repo)}:{lineno}", line))
    return out


# --- .claude/settings*.json permissions -----------------------------------------------------


def claude_settings_entries(repo: Path = REPO) -> list[tuple[str, str]]:
    """(location, permission string) for every entry in a `.claude/settings*.json` allow/deny/ask list."""
    out = []
    for f in sorted(repo.glob(".claude/settings*.json")):
        data = json.loads(f.read_text())
        perms = data.get("permissions", {})
        for bucket in ("allow", "deny", "ask"):
            for entry in perms.get(bucket, []) or []:
                out.append((f"{f.relative_to(repo)} permissions.{bucket}", entry))
    return out
