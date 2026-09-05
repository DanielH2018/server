"""`scripts/validate/vale.sh` provisions the Google style package exactly once, under a lock.

prek splits a hook's file list across several CONCURRENT invocations — ten for `--all-files`
in this repo. Before the lock, all ten started in a fresh worktree with `styles/Google`
missing, all ten ran `vale sync` into that one directory, and one died with
`unlinkat .../styles/Google: directory not empty` while a sibling was still unpacking. Every
invocation reported `0 errors`: the hook failed on the sync alone (issue #1189).

WHAT MAKES THIS TEST ABLE TO GO RED. `test_an_unlocked_guard_syncs_concurrently` runs the
pre-fix one-liner against the same harness and asserts it DOES race. A count of one sync
proves nothing on its own — a script that never syncs, or one prek happened to run serially,
scores the same. So each test here pins a property: how many syncs ran, whether their windows
overlapped, and that the lint still ran for every invocation.

`vale` is faked on PATH. It is not in leakguard's SHIMMED_BINARIES, so nothing else
intercepts it, and the fake records a timestamped start/end for every `sync` — which is what
lets the overlap assertion read the race directly rather than inferring it from a count.

Run: uv run pytest scripts/validate/tests/test_vale_sync_guard.py
"""

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GUARD = REPO / "scripts" / "validate" / "vale.sh"
PREK_TOML = REPO / "prek.toml"

# The pre-fix hook entry, kept verbatim as the rejecting input. It is the thing the lock
# replaced, so it is the only honest proof the harness can see the race at all.
UNLOCKED_GUARD = """#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo"
[ -d styles/Google ] || vale sync || exit 1
exec vale "$@"
"""

# Sleeps long enough that ten processes started back to back overlap if nothing serialises
# them, and short enough to keep the suite fast. The unlocked variant races on every run.
FAKE_VALE = """#!/usr/bin/env bash
if [ "${1:-}" = "sync" ]; then
  printf 'sync-start %s\\n' "$(date +%s%N)" >>"$VALE_LOG"
  sleep 0.3
  mkdir -p styles/Google
  printf 'rules\\n' >styles/Google/Google.yml
  printf 'sync-end %s\\n' "$(date +%s%N)" >>"$VALE_LOG"
  exit 0
fi
printf 'lint %s\\n' "$*" >>"$VALE_LOG"
exit 0
"""

# Deliberately not in `releases/download/v<version>/` shape: `test_renovate_release_urls.py`
# reads every tracked file for that pattern and demands a Renovate manager scan the file.
# This is a fixture, not a pin.
PACKAGES_URL = "https://example.invalid/style-packages/Google-v0.7.1.zip"


@pytest.fixture
def fake_repo(tmp_path):
    """A throwaway checkout holding the real guard, a fake `vale`, and a pinned .vale.ini.

    Everything is written under tmp_path: the suite runs with `-n auto`, and a test that
    provisioned the real styles/ would be shared state across workers.
    """
    (tmp_path / "scripts" / "validate").mkdir(parents=True)
    shutil.copy2(GUARD, tmp_path / "scripts" / "validate" / "vale.sh")
    (tmp_path / ".vale.ini").write_text(
        f"StylesPath = styles\nMinAlertLevel = error\nPackages = {PACKAGES_URL}\n"
    )
    (tmp_path / "styles" / "Homelab").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    for name in ("a.md", "b.md"):
        (tmp_path / "docs" / name).write_text("# heading\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    vale = bin_dir / "vale"
    vale.write_text(FAKE_VALE)
    vale.chmod(0o755)
    return tmp_path


def env_for(repo: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{repo / 'bin'}{os.pathsep}{env['PATH']}"
    env["VALE_LOG"] = str(repo / "vale.log")
    return env


def run_guard(repo: Path, *args: str, script: str = "scripts/validate/vale.sh"):
    return subprocess.run(
        [str(repo / script), *args],
        cwd=repo,
        env=env_for(repo),
        capture_output=True,
        text=True,
    )


def run_concurrently(repo: Path, count: int, script: str = "scripts/validate/vale.sh"):
    """Start `count` guards at once and wait for all of them — prek's own shape."""
    env = env_for(repo)
    procs = [
        subprocess.Popen(
            [str(repo / script), f"docs/{n}.md"],
            cwd=repo,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(count)
    ]
    results = []
    for proc in procs:
        _, stderr = proc.communicate()
        results.append((proc.returncode, stderr))
    return results


def events(repo: Path) -> list[tuple[str, int]]:
    log = repo / "vale.log"
    if not log.exists():
        return []
    parsed = []
    for line in log.read_text().splitlines():
        kind, _, rest = line.partition(" ")
        parsed.append((kind, int(rest) if re.fullmatch(r"\d+", rest) else 0))
    return parsed


def sync_windows(repo: Path) -> list[tuple[int, int]]:
    """(start, end) nanosecond pairs, one per completed `vale sync`."""
    starts = [ts for kind, ts in events(repo) if kind == "sync-start"]
    ends = [ts for kind, ts in events(repo) if kind == "sync-end"]
    assert len(starts) == len(ends), f"unfinished sync in {events(repo)}"
    return list(zip(sorted(starts), sorted(ends), strict=True))


def overlapping(windows: list[tuple[int, int]]) -> bool:
    ordered = sorted(windows)
    return any(b[0] < a[1] for a, b in zip(ordered, ordered[1:], strict=False))


def count(repo: Path, kind: str) -> int:
    return sum(1 for k, _ in events(repo) if k == kind)


def test_ten_concurrent_invocations_sync_once_and_all_lint(fake_repo):
    """The accepting input: prek's ten parallel invocations of a fresh worktree."""
    results = run_concurrently(fake_repo, 10)

    assert all(rc == 0 for rc, _ in results), results
    assert count(fake_repo, "sync-start") == 1, events(fake_repo)
    # Sync-count alone would also pass for a guard that never reached Vale.
    assert count(fake_repo, "lint") == 10, events(fake_repo)
    assert (fake_repo / "styles" / "Google" / ".synced").read_text().strip() == (
        f"Packages = {PACKAGES_URL}"
    )


def test_an_unlocked_guard_syncs_concurrently(fake_repo):
    """The rejecting input: the pre-fix entry, which is what issue #1189 reported.

    Without this the assertions above are unfalsifiable — a harness that serialises the
    processes by accident would score a clean single sync for any script at all.
    """
    unlocked = fake_repo / "scripts" / "validate" / "unlocked.sh"
    unlocked.write_text(UNLOCKED_GUARD)
    unlocked.chmod(0o755)

    results = run_concurrently(fake_repo, 10, script="scripts/validate/unlocked.sh")

    assert all(rc == 0 for rc, _ in results), results
    assert count(fake_repo, "sync-start") > 1, events(fake_repo)
    assert overlapping(sync_windows(fake_repo)), (
        "the unlocked guard's syncs did not overlap — the harness cannot see the race "
        f"this test exists to prove: {sync_windows(fake_repo)}"
    )


def test_the_locked_syncs_never_overlap(fake_repo):
    """The property the lock provides, stated directly rather than as a count."""
    run_concurrently(fake_repo, 10)
    assert not overlapping(sync_windows(fake_repo)), sync_windows(fake_repo)


def test_a_provisioned_worktree_does_not_resync(fake_repo):
    """`vale sync` re-downloads unconditionally, so a second commit must not pay for it."""
    assert run_guard(fake_repo, "docs/a.md").returncode == 0
    assert run_guard(fake_repo, "docs/b.md").returncode == 0
    assert count(fake_repo, "sync-start") == 1, events(fake_repo)
    assert count(fake_repo, "lint") == 2, events(fake_repo)


def test_a_half_unpacked_package_is_resynced(fake_repo):
    """A partial tree passes `[ -d styles/Google ]` and lints with a partial rule set.

    That is a green hook checking less than it claims, which is why the stamp — written only
    after a sync exits 0 — is the guard rather than the directory.
    """
    partial = fake_repo / "styles" / "Google"
    partial.mkdir(parents=True)
    (partial / "leftover.yml").write_text("half an unpack\n")

    assert run_guard(fake_repo, "docs/a.md").returncode == 0

    assert count(fake_repo, "sync-start") == 1, events(fake_repo)
    assert not (partial / "leftover.yml").exists(), (
        "the partial tree survived the re-sync — `rm -rf` is what keeps `unlinkat` from "
        "tripping over a directory that is not empty"
    )


def test_a_package_url_bump_resyncs(fake_repo):
    """Renovate bumps the pinned release URL; a worktree must not stay on the old package."""
    assert run_guard(fake_repo, "docs/a.md").returncode == 0
    ini = fake_repo / ".vale.ini"
    ini.write_text(ini.read_text().replace("v0.7.1", "v0.8.0"))

    assert run_guard(fake_repo, "docs/a.md").returncode == 0

    assert count(fake_repo, "sync-start") == 2, events(fake_repo)


def test_no_filenames_provisions_and_exits_clean(fake_repo):
    """CI's install step calls the guard with no arguments, before it runs the hooks."""
    result = run_guard(fake_repo)

    assert result.returncode == 0, result.stderr
    assert count(fake_repo, "sync-start") == 1, events(fake_repo)
    assert count(fake_repo, "lint") == 0, events(fake_repo)


def test_a_vale_ini_with_no_pinned_package_fails_loudly(fake_repo):
    """The stamp is keyed on the pinned line; without one the guard must not sync blind."""
    (fake_repo / ".vale.ini").write_text("StylesPath = styles\n")

    result = run_guard(fake_repo, "docs/a.md")

    assert result.returncode != 0
    assert "Packages" in result.stderr, result.stderr
    assert count(fake_repo, "sync-start") == 0, events(fake_repo)


def test_the_prek_hook_runs_this_script():
    """The hook entry and the script have to stay pointed at each other."""
    config = tomllib.loads(PREK_TOML.read_text())
    entries = [
        hook["entry"]
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == "vale"
    ]
    assert entries == ["scripts/validate/vale.sh"], entries
    assert os.access(GUARD, os.X_OK), (
        f"{GUARD} is not executable — prek would fail the hook with a permission error"
    )


def test_the_ci_workflow_provisions_through_the_same_script():
    """CI installs the package before the hooks run; a bare `vale sync` there would skip
    the stamp and leave the first hook invocation re-fetching it."""
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "scripts/validate/vale.sh" in workflow
    assert not re.search(r"^\s+vale sync\s*$", workflow, re.MULTILINE), (
        "CI still calls `vale sync` directly; it writes no stamp"
    )
