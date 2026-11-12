"""Where a `scripts/...` path can be executed from, read once for two different questions.

`scripts/docs/reference/scripts.py` classifies HOW a script is run (scheduled, gated,
library, adhoc) so the generated reference page can say so. `scripts/test_invoker_paths_resolve.py`
asserts every `scripts/...` token an invoker names still resolves to a real file, so a rename
that misses one of these sites fails a test instead of a 3am cron. Both walk the same places —
`prek.toml`, the GitHub Actions workflows, systemd units, `.claude/hooks` wrappers and
`.claude/settings*.json` permissions — and until now each carried its own copy of "which
files, which field", which is exactly the kind of fact that drifts when only one copy gets
updated after a reorg.

WHAT LIVES HERE VS. WHAT DOESN'T. This module owns file discovery and field extraction — WHICH
files count as an invocation site, and WHICH field of each one is the text that actually runs.
It does not decide what counts as a reference inside that text; a caller regexes the returned
text for its own purpose (a bare `scripts/...` token for the guard test, a run-context-aware
scan that also tolerates comments and mentions for the generator's classifier). Every source
lives here, but not every source has two consumers: `prek_hook_entries` and `workflow_run_steps`
are read only by the guard test today. `reference/scripts.py`'s classifier keeps its own
whole-file scan of `prek.toml` and each workflow instead of the field-scoped one, because it
deliberately reads more than the `entry=`/`run:` field alone — its own test
(`test_an_argv_element_in_python_source_is_an_invocation`) pins a prek.toml hook keyed
`other = "..."` as a valid invocation, which a strict `entry=`-only extraction would miss.
Field-scoping it to match the guard test would narrow the classifier's coverage.

Import it through the same bootstrap as any other cross-directory `lib` import (see
`.claude/rules/python-layout.md`)::

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from lib.invocation_sites import workflow_files  # noqa: E402
"""

import json
import tomllib
from pathlib import Path


import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.repo_paths import REPO


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
        data = yaml_fast.safe_load(wf.read_text()) or {}
        for job_name, job in (data.get("jobs") or {}).items():
            for i, step in enumerate(job.get("steps") or []):
                run = step.get("run")
                if run:
                    loc = f"{wf.relative_to(repo)} job {job_name!r} step {i}"
                    out.append((loc, run))
    return out


# --- .claude/hooks wrappers ------------------------------------------------------------------


def claude_hook_files(repo: Path = REPO) -> list[Path]:
    """Every non-test `.claude/hooks` wrapper — a script this repo runs on its own commands."""
    hooks_dir = repo / ".claude" / "hooks"
    files = sorted(hooks_dir.glob("*.sh")) + sorted(hooks_dir.glob("*.py"))
    return [f for f in files if f.is_file() and not f.name.startswith("test_")]


# --- systemd units -----------------------------------------------------------------------


def systemd_exec_lines(repo: Path = REPO) -> list[tuple[str, str]]:
    """(location, line text) for every `ExecStart*=` line in a systemd `*.service.j2`.

    Covers the setup roles and the host components, whose units run some tooling from the
    checkout (`homelab-etcd-restore-drill.service`).
    """
    out = []
    units = [
        *repo.glob("ansible/roles/**/*.service.j2"),
        *repo.glob("hosts/components/**/*.service.j2"),
    ]
    for svc in sorted(units):
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
