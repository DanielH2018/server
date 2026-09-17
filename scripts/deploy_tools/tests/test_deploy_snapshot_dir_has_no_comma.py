#!/usr/bin/env python3
"""A multi-tag `deploy.sh` snapshot directory must carry no comma.

The snapshot directory becomes the deploy's cwd, and ansible.cfg's `inventory =` setting is a
relative path. ansible-core resolves that path against cwd and treats any comma in the
resolved string as an inline comma-separated host list rather than a file path -- so a snapshot
named `authelia,traefik-<stamp>` breaks inventory parsing with `Origin.path must be … instead
of … _AnsibleTaggedStr`, and the deploy reports "no hosts matched" while exiting 0. Reproduced
2026-09-16 against the real ansible-core in this repo's `.venv`; a single tag (no comma in the
directory name) and an explicit `-i` both work.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_snapshot_dir_has_no_comma.py
"""

import subprocess
from pathlib import Path

from _deploy_sh_fakes import FAKE_RECAP, FLOCK_STUB, deploy_sh_env, make_snapshot_repo

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"

# Records the snapshot's working directory at the moment the wrapper would invoke
# ansible-playbook there -- the one thing this bug depends on.
_UV_STUB = """#!/bin/bash
case "$*" in
  *ansible-playbook*) pwd > "$DEPLOY_TEST_CWD_FILE"; {recap}; exit 0 ;;
  *deploy_tags.py\\ validate*) exit 0 ;;
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP)


def test_multi_tag_snapshot_dir_has_no_comma(tmp_path: Path) -> None:
    repo = make_snapshot_repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "flock").write_text(FLOCK_STUB)
    (bin_dir / "uv").write_text(_UV_STUB)
    for stub in ("flock", "uv"):
        (bin_dir / stub).chmod(0o755)

    cwd_file = tmp_path / "snapshot-cwd"
    env = deploy_sh_env(tmp_path, bin_dir, DEPLOY_TEST_CWD_FILE=str(cwd_file))

    result = subprocess.run(
        [str(_DEPLOY_SH), "--tags", "authelia,traefik"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    snapshot_dir = cwd_file.read_text().strip()
    assert snapshot_dir, "ansible-playbook was never invoked"
    assert "," not in snapshot_dir, (
        "snapshot dir carries a literal comma, which ansible-core's inventory-source "
        f"resolution misreads as an inline host list: {snapshot_dir}"
    )


def test_single_tag_snapshot_dir_is_unaffected(tmp_path: Path) -> None:
    """A one-tag deploy never had a comma to begin with; this pins the label, not just its fix."""
    repo = make_snapshot_repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "flock").write_text(FLOCK_STUB)
    (bin_dir / "uv").write_text(_UV_STUB)
    for stub in ("flock", "uv"):
        (bin_dir / stub).chmod(0o755)

    cwd_file = tmp_path / "snapshot-cwd"
    env = deploy_sh_env(tmp_path, bin_dir, DEPLOY_TEST_CWD_FILE=str(cwd_file))

    result = subprocess.run(
        [str(_DEPLOY_SH), "--tags", "authelia"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    snapshot_dir = cwd_file.read_text().strip()
    assert "authelia" in Path(snapshot_dir).name
