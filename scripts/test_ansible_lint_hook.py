"""The ansible-lint prek hook must lint nothing when prek hands it no filenames.

prek runs the hook even when no staged file matches its `files:` regex. `ansible-lint` with
no paths does not lint nothing -- it walks the filesystem. A commit touching only docs
therefore got a full-tree lint of ~1,600 files including untracked and gitignored ones, and
any violation among them rejected the commit.

That is not hypothetical. It is why `docs-refresh.sh` could not commit: it stages only
`docs/reference` and `docs/assets/generated`, so this hook received zero filenames, walked,
failed, and the script reset -- leaving the tree dirty, which parked gitops-deploy behind
origin until someone cleaned it by hand.

These tests run the hook's real entry string out of prek.toml. The stubs make the
pass-through case fast and offline; the zero-arg case needs no stubs, because the whole point
is that nothing should be invoked at all.
"""

import os
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREK_TOML = REPO / "prek.toml"


def ansible_lint_hook() -> dict:
    config = tomllib.loads(PREK_TOML.read_text())
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo.get("hooks", [])
        if hook.get("id") == "ansible-lint"
    ]
    assert len(hooks) == 1, (
        f"expected exactly one ansible-lint hook, found {len(hooks)}"
    )
    return hooks[0]


def _stub_bin(tmp_path: Path) -> Path:
    """Fake `ansible-galaxy` and `ansible-lint` that record the args they were given.

    Running the real pair would install collections over the network and lint the tree,
    which makes the test slow enough to be skipped and dependent on the host's state.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for name in ("ansible-galaxy", "ansible-lint"):
        script = stub_dir / name
        script.write_text(f'#!/bin/sh\necho "{name} $*" >> "$STUB_LOG"\nexit 0\n')
        script.chmod(0o755)
    return stub_dir


def _run(entry: str, args: list[str], tmp_path: Path) -> tuple[int, str]:
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{_stub_bin(tmp_path)}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
    }
    completed = subprocess.run(
        f"{entry} {' '.join(args)}",
        shell=True,
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return completed.returncode, log.read_text()


def test_zero_filenames_invokes_nothing(tmp_path):
    """The bug: with no args the entry used to reach ansible-lint, which then walked."""
    entry = ansible_lint_hook()["entry"]
    rc, calls = _run(entry, [], tmp_path)
    assert rc == 0, f"entry exited {rc} with no filenames"
    assert calls == "", (
        f"with zero filenames the hook must invoke nothing, but ran: {calls!r}. "
        f"ansible-lint with no paths walks the filesystem instead of linting nothing."
    )


def test_filenames_are_passed_through(tmp_path):
    """The guard must not become a hook that never lints anything."""
    entry = ansible_lint_hook()["entry"]
    rc, calls = _run(entry, ["ansible/deploy.yml", "ansible/site.yml"], tmp_path)
    assert rc == 0, f"entry exited {rc}: {calls!r}"
    assert "ansible-galaxy" in calls, f"collections install was skipped: {calls!r}"
    lint_calls = [
        line for line in calls.splitlines() if line.startswith("ansible-lint ")
    ]
    assert len(lint_calls) == 1, f"expected one ansible-lint call, got {calls!r}"
    assert "ansible/deploy.yml" in lint_calls[0], lint_calls[0]
    assert "ansible/site.yml" in lint_calls[0], lint_calls[0]


def test_the_hook_still_declares_its_scope():
    """A `files:` regex that matched nothing would make the zero-arg path the only path."""
    hook = ansible_lint_hook()
    assert hook["files"] == "^ansible/.*\\.ya?ml$", hook["files"]
    assert hook["pass_filenames"] is True
    assert hook["require_serial"] is True
