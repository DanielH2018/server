"""code-server's extensions.sh installs only the VSIX versions the config volume lacks.

Each test runs the real script against a fake `code-server` CLI that lists a fixed set of
installed extensions, logs each call, and refuses a reinstall of a listed version the way the
real CLI does ("Please restart VS Code before reinstalling").
"""

from pathlib import Path

from _helpers import K8S_ROLES
from lib.proc_testing import run, write_exec

SCRIPT = K8S_ROLES / "code-server" / "files" / "extensions.sh"

FAKE_CLI = """#!/bin/bash
echo "$*" >> "{log}"
case "$*" in
  *--list-extensions*) cat "{installed}" ;;
  *--install-extension*)
    vsix="${{@: -1}}"
    case "$vsix" in *broken*) echo "Failed Installing Extensions" >&2; exit 1 ;; esac
    if grep -qFxi "$(basename "$vsix" .vsix)" "{installed}"; then
      echo "Error: Please restart VS Code before reinstalling" >&2; exit 1
    fi ;;
esac
"""


def _run(
    tmp_path: Path, vsix: list[str], installed: list[str]
) -> tuple[int, list[str]]:
    """Run extensions.sh with `vsix` baked and `installed` listed; return (exit, installs)."""
    log = tmp_path / "calls.log"
    listed = tmp_path / "installed.txt"
    listed.write_text("".join(f"{line}\n" for line in installed))
    cli = write_exec(
        tmp_path / "code-server", FAKE_CLI.format(log=log, installed=listed)
    )
    vsix_dir = tmp_path / "vsix"
    vsix_dir.mkdir()
    for name in vsix:
        (vsix_dir / f"{name}.vsix").write_bytes(b"")
    done = run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": "/usr/bin:/bin",
            "CODE_SERVER": str(cli),
            "VSIX_DIR": str(vsix_dir),
        },
    )
    installs = [
        Path(line.split()[-1]).stem
        for line in log.read_text().splitlines()
        if "--install-extension" in line
    ]
    return done.returncode, installs


def test_an_unchanged_extension_is_skipped_and_the_step_succeeds(
    tmp_path: Path,
) -> None:
    code, installs = _run(
        tmp_path,
        vsix=["charliermarsh.ruff@2026.82.0", "Anthropic.claude-code@2.1.325"],
        installed=["charliermarsh.ruff@2026.82.0", "anthropic.claude-code@2.1.325"],
    )
    assert (code, installs) == (0, [])


def test_only_a_changed_version_is_installed(tmp_path: Path) -> None:
    code, installs = _run(
        tmp_path,
        vsix=["charliermarsh.ruff@2026.82.0", "ms-python.python@2026.4.0"],
        installed=["charliermarsh.ruff@2026.82.0", "ms-python.python@2026.2.0"],
    )
    assert (code, installs) == (0, ["ms-python.python@2026.4.0"])


def test_a_failed_install_fails_the_step(tmp_path: Path) -> None:
    code, installs = _run(
        tmp_path,
        vsix=["broken.ext@1.0.0", "ms-python.python@2026.4.0"],
        installed=[],
    )
    assert code == 1
    assert sorted(installs) == ["broken.ext@1.0.0", "ms-python.python@2026.4.0"]
