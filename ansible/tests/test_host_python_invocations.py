"""Host Python runs through uv, on a pinned interpreter, and only that way.

This replaces test_host_scripts_py312.py. That guard existed because the hosts ran Ubuntu 24.04's
Python 3.12 while the repo was on 3.14, so 3.13+ syntax parsed in CI and SyntaxErrored on the
host — silently, in the case of session-health.py, whose wrapper routes stderr to /dev/null and
exits 0 by design. Those scripts now run a pinned 3.14 through uv and the floor is gone.

What remains dangerous is the way back. A new unit, cron entry or hook wrapper reaching for
`/usr/bin/python3` puts one script under 3.12 again with nothing left to notice. So this file
guards the properties the migration established rather than the syntax level it removed.

Container contexts are deliberately out of scope: a Dockerfile or compose healthcheck names the
interpreter inside its own digest-pinned image, which has nothing to do with the host.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SEARCH_ROOTS = [_REPO / "ansible/roles", _REPO / ".claude/hooks"]
_SUFFIXES = {".j2", ".yml", ".sh"}

# A container's own interpreter, not the host's.
_CONTAINER_CONTEXT = re.compile(
    r"Dockerfile|docker-compose|deployment\.yaml|healthcheck"
)

# The one invocation that deliberately keeps uv's self-healing download. See the comment above its
# ExecStart: hard-failing the deploy pipeline on a missing interpreter removes the machine that
# would ship the fix.
_DOWNLOADS_EXEMPT = "gitops-deploy.service.j2"


def _candidate_files():
    for root in _SEARCH_ROOTS:
        for path in root.rglob("*"):
            if path.suffix not in _SUFFIXES or not path.is_file():
                continue
            if "archive" in path.parts or _CONTAINER_CONTEXT.search(str(path)):
                continue
            yield path


def _offending_lines(needle: str):
    for path in _candidate_files():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or _CONTAINER_CONTEXT.search(line):
                continue
            if needle in line:
                yield f"{path.relative_to(_REPO)}:{n}: {stripped[:100]}"


def _pinned_uv_lines():
    """Only the host-script invocations this migration created.

    The discriminator is `--python`. A `uv run` that names an interpreter is a standalone host
    script and must not touch a project; a `uv run` without one is deliberately using the repo
    project's environment because it needs repo dependencies. The repo has several of the latter
    and they are all correct — auto-approve-readonly.sh, block-protected-edits.sh,
    auto-approve-remote-ssh.sh (`--no-sync --quiet python`), redeploy_cron.yml and
    secret-rotation-audit.sh.j2 (`--frozen`). An earlier draft of this guard scanned every
    `uv run` line and would have failed on all of them.
    """
    for path in _candidate_files():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if (
                stripped.startswith("#")
                or "uv run" not in line
                or "--python" not in line
            ):
                continue
            yield path, n, stripped


def test_no_host_invocation_uses_the_system_interpreter():
    offenders = sorted(_offending_lines("/usr/bin/python3"))
    assert not offenders, (
        "these invoke the host's system Python (Ubuntu 24.04's 3.12) instead of running through "
        "uv on the pinned interpreter. That silently puts a script back below the repo's syntax "
        "floor:\n  " + "\n  ".join(offenders)
    )


def test_no_host_invocation_names_an_interpreter_directly():
    """Publishing or calling a python3.14 path would be a second way to run Python here."""
    offenders = sorted(_offending_lines("/usr/local/bin/python3"))
    assert not offenders, (
        "these name an interpreter directly instead of going through `uv run`. uv is the single "
        "entry point for Python on these hosts:\n  " + "\n  ".join(offenders)
    )


def test_pinned_uv_invocations_disable_project_discovery():
    """`uv run` searches upward for a project from its working directory, and systemd and cron
    have arbitrary ones. gitops-deploy's WorkingDirectory is itself a uv project. --no-project
    stops uv resolving and syncing that project from a unit that holds the git-tree lock.

    Measured 2026-08-16, so do not overstate what this defends: with an explicit --python the
    version is honoured either way, and --no-project does not even suppress `.venv` activation
    from the cwd. It governs project SYNCING. The version guarantee comes from --python.
    """
    offenders = [
        f"{p.relative_to(_REPO)}:{n}: {s[:100]}"
        for p, n, s in _pinned_uv_lines()
        if "--no-project" not in s
    ]
    assert not offenders, (
        "`uv run --python` without --no-project resolves whatever project the working directory "
        "happens to sit in:\n  " + "\n  ".join(sorted(offenders))
    )


def test_the_pin_is_enforced_everywhere_except_the_deploy_pipeline():
    """uv's python-downloads default is `automatic`, so `--python 3.14.6` against a host lacking
    that build fetches one rather than failing — making the pin a request, not enforcement.
    `--no-python-downloads` closes that, and every host invocation carries it except one.

    gitops-deploy is exempt on purpose: there, the hard failure would be the deploy pipeline
    refusing to start, which removes the ability to ship the correction. This test pins BOTH
    halves, so neither the rule nor its exception can drift silently.
    """
    missing = [
        f"{p.relative_to(_REPO)}:{n}: {s[:100]}"
        for p, n, s in _pinned_uv_lines()
        if p.name != _DOWNLOADS_EXEMPT and "--no-python-downloads" not in s
    ]
    assert not missing, (
        "these pin an interpreter but let uv silently download it if absent, so the pin is not "
        "enforced:\n  " + "\n  ".join(sorted(missing))
    )

    exempt = [(p, s) for p, _, s in _pinned_uv_lines() if p.name == _DOWNLOADS_EXEMPT]
    assert exempt, (
        f"{_DOWNLOADS_EXEMPT} no longer has a pinned uv invocation. If that unit was renamed or "
        "retired, update _DOWNLOADS_EXEMPT — do not leave a stale exemption behind."
    )
    assert all("--no-python-downloads" not in s for _, s in exempt), (
        f"{_DOWNLOADS_EXEMPT} has gained --no-python-downloads. That was excluded deliberately: a "
        "missing interpreter would stop the deploy pipeline, which is the machine that ships the "
        "fix. If this is now wanted, remove the exemption here and the comment on its ExecStart."
    )


def test_the_hooks_use_the_uv_that_exists_on_both_hosts():
    """There are exactly two correct absolute uv paths, and picking the wrong one fails silently.

    `/usr/local/bin/uv` is a symlink created on daniel-box only. The Claude hooks are the one
    group that also runs on daniel-server, where that path does not exist (verified 2026-08-16:
    `ls -l /usr/local/bin/uv` -> "No such file or directory"), so they must use
    `/home/<user>/.local/bin/uv`, which exists on both and is what their sibling hooks already
    use. The hooks route stderr to /dev/null and exit 0 by design, so the wrong path there is
    invisible — this test is the only thing that would notice.
    """
    offenders = [
        f"{p.name}:{n}: {line.strip()[:100]}"
        for p in sorted((_REPO / ".claude/hooks").glob("*.sh"))
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if "/usr/local/bin/uv" in line and not line.strip().startswith("#")
    ]
    assert not offenders, (
        "the Claude hooks run on daniel-server too, where /usr/local/bin/uv does not exist. Use "
        "/home/<user>/.local/bin/uv:\n  " + "\n  ".join(offenders)
    )


def test_hook_wrappers_pin_the_same_version_as_ansible():
    """The hooks are plain shell, not Ansible templates, so they carry the version as a literal
    and cannot interpolate host_python_version. Nothing else couples the two, so a bump to the
    Ansible pin would leave the hooks silently requesting an interpreter the hosts no longer
    install — and these are the wrappers that route stderr to /dev/null."""
    import yaml

    pin = yaml.safe_load((_REPO / "ansible/inventory/group_vars/all.yml").read_text())[
        "host_python_version"
    ]
    offenders = []
    for path in sorted((_REPO / ".claude/hooks").glob("*.sh")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if "--python" not in stripped or stripped.startswith("#"):
                continue
            if f"--python {pin}" not in stripped:
                offenders.append(f"{path.name}:{n}: {stripped[:100]}")

    assert not offenders, (
        f"these request an interpreter other than the Ansible pin ({pin}, from "
        "ansible/inventory/group_vars/all.yml):\n  " + "\n  ".join(offenders)
    )
