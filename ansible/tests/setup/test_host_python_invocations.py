"""Host Python runs through uv, on a pinned interpreter, and only that way.

Hosts run Ubuntu 24.04's Python 3.12 as the system interpreter while the repo is on 3.14, so a
script run by `/usr/bin/python3` parses 3.13+ syntax in CI and SyntaxErrors on the host --
silently, in the case of session-health.py, whose wrapper routes stderr to /dev/null and
exits 0 by design. Host scripts therefore run a pinned 3.14 through uv. A new unit, cron
entry or hook wrapper reaching for `/usr/bin/python3` puts one script under 3.12 with nothing
left to notice, so this file guards that route rather than the syntax level.

Container contexts are deliberately out of scope: a Dockerfile or compose healthcheck names the
interpreter inside its own digest-pinned image, which has nothing to do with the host.
"""

import re
from _helpers import REPO as _REPO, load_tasks, walk_tasks
from _role_census import task_files

from lib import role_installs
from lib.repo_paths import ALL_VARS, ROLES


# The same export the root crons' templates carry (cron_checks.py enforces it there). A task
# with `become: true` runs with HOME=/root, and uv finds no managed Python under /root: the pin
# is installed `become: false` under the connecting user.
_ROOT_UV_ENV = "UV_PYTHON_INSTALL_DIR"
_COMMAND_MODULES = ("ansible.builtin.command", "ansible.builtin.shell")


def _command_text(task: dict) -> str:
    """The command a task runs, for the string, `cmd:` and `argv:` shapes alike."""
    for key in _COMMAND_MODULES:
        module = task.get(key)
        if isinstance(module, str):
            return module
        if isinstance(module, dict):
            return str(
                module.get("cmd") or " ".join(map(str, module.get("argv") or []))
            )
    return ""


def _root_uv_task_missing_install_dir(task: dict) -> bool:
    """A `become: true` task that runs a pinned `uv run` without pointing uv at the user's
    interpreters. Only the task's own `become:` is read; no task file here sets it on a block
    that wraps a uv command."""
    command = _command_text(task)
    if (
        task.get("become") is not True
        or "uv" not in command
        or "--python" not in command
    ):
        return False
    return _ROOT_UV_ENV not in (task.get("environment") or {})


def _root_uv_tasks():
    for path in task_files():
        for task in walk_tasks(load_tasks(path)):
            command = _command_text(task)
            if task.get("become") is True and "uv" in command and "--python" in command:
                yield path.relative_to(_REPO), task


def test_root_uv_tasks_point_uv_at_the_user_interpreters():
    found = list(_root_uv_tasks())
    names = {task.get("name", "") for _, task in found}
    # Non-vacuous: the release-prune task must be in the census.
    assert "Prune superseded releases for {{ release_bin_group }}" in names
    offenders = [
        f"{path}: {task.get('name')}"
        for path, task in found
        if _root_uv_task_missing_install_dir(task)
    ]
    assert not offenders, (
        f"these run a pinned `uv run` as root without `environment: {_ROOT_UV_ENV}`, so uv "
        "searches /root and finds no interpreter:\n  " + "\n  ".join(offenders)
    )


def test_root_uv_task_with_the_install_dir_is_clean():
    task = {
        "ansible.builtin.command": {
            "argv": ["/usr/local/bin/uv", "run", "--python", "3.14.6"]
        },
        "become": True,
        "environment": {_ROOT_UV_ENV: "/home/ubuntu/.local/share/uv/python"},
    }
    assert not _root_uv_task_missing_install_dir(task)


def test_root_uv_task_without_the_install_dir_is_flagged():
    task = {
        "ansible.builtin.command": {
            "argv": ["/usr/local/bin/uv", "run", "--python", "3.14.6"]
        },
        "become": True,
    }
    assert _root_uv_task_missing_install_dir(task)


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
            # Match the repo-relative path, never the absolute one: a checkout directory
            # carrying one of these words (a worktree named for the healthchecks service, say)
            # would otherwise exclude every file and leave BOTH assertions below vacuously
            # true. DECIDED: relative_to(_REPO), not a suffix match -- the container contexts
            # this skips are named by their in-repo path.
            rel = path.relative_to(_REPO)
            if "archive" in rel.parts or _CONTAINER_CONTEXT.search(str(rel)):
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
    """Only the host-script invocations.

    The discriminator is `--python`. A `uv run` that names an interpreter is a standalone host
    script and must not touch a project; a `uv run` without one is deliberately using the repo
    project's environment because it needs repo dependencies. The repo has several of the latter
    and they are all correct — run-hook.sh's `--project` arm
    (`--no-sync --quiet python`) and secret-rotation-audit.sh.j2
    (`--frozen`).
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

    Measured, so do not overstate what this defends: with an explicit --python the
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


def test_the_hooks_use_the_uv_that_exists_without_the_tooling_tag():
    """There are exactly two correct absolute uv paths, and picking the wrong one fails silently.

    `/usr/local/bin/uv` is a copy the `tooling` tag of `initial_setup` publishes, gated on
    `dev_tooling_hosts` (daniel-box and daniel-server, `group_vars/all.yml`). It exists on both
    prod nodes, copied from `/home/ubuntu/.local/bin/uv`. The Claude hooks avoid it anyway. They
    must also work on a host that never ran the `tooling` tag, and `/home/<user>/.local/bin/uv`
    exists wherever uv is installed without depending on a `become: true` copy task having
    run. It is what their sibling hooks already use. The hooks route stderr to /dev/null and
    exit 0 by design, so the wrong path there is invisible — this test is the only thing that
    would notice.
    """
    offenders = [
        f"{p.name}:{n}: {line.strip()[:100]}"
        for p in sorted((_REPO / ".claude/hooks").glob("*.sh"))
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if "/usr/local/bin/uv" in line and not line.strip().startswith("#")
    ]
    assert not offenders, (
        "the Claude hooks must work on a host that never ran the `tooling` tag, which is what "
        "creates /usr/local/bin/uv. Use /home/<user>/.local/bin/uv:\n  "
        + "\n  ".join(offenders)
    )


def test_hook_wrappers_pin_the_same_version_as_ansible():
    """The hooks are plain shell, not Ansible templates, so they carry the version as a literal
    and cannot interpolate host_python_version. Nothing else couples the two, so a bump to the
    Ansible pin would leave the hooks silently requesting an interpreter the hosts no longer
    install — and these are the wrappers that route stderr to /dev/null."""
    import yaml

    pin = yaml.safe_load(ALL_VARS.read_text())["host_python_version"]
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


# A command word: the start of a line that does not continue the one above, or what follows a
# substitution, a pipe, a list operator, `exec`, a systemd ExecStart= or a cron `job:`.
_COMMAND_POSITION = (
    r"""(?:^(?<!\\\n)|\$\(|<\(|[;|&]|\bexec\s|ExecStart=[-@+!:]*|\bjob:\s*["']?)\s*"""
)


def _shebang_runs(text: str, installed: list[str]) -> list[str]:
    """The installed `.py` paths `text` runs as a command word rather than as uv's argument.

    A path `role_installs` reads as a `--no-project` command's script is uv's argument, so a
    YAML folded scalar that puts the path at the start of its own line is not a command word.
    """
    if not installed:
        return []
    pattern = re.compile(
        _COMMAND_POSITION + "(" + "|".join(map(re.escape, installed)) + r")(?![\w.-])",
        re.MULTILINE,
    )
    through_uv = set(role_installs.no_project_spans(text))
    return [m.group(1) for m in pattern.finditer(text) if m.span(1) not in through_uv]


def test_no_installed_script_runs_by_its_shebang():
    """A role script's `#!/usr/bin/env python3` is the host's system 3.12, so running it by path
    escapes the pin the tests above enforce on command strings. #4489 was one:
    manifest-prune-check ran manifest-declares.py by path, behind `2>/dev/null`."""
    installed = sorted(live for live in role_installs.install_map() if "{{" not in live)
    # Non-vacuous: the script #4489 named must be in the census this scans for.
    assert "/usr/local/bin/manifest-declares.py" in installed
    offenders = [
        f"{path.relative_to(_REPO)}: {live}"
        for path in sorted(ROLES.rglob("*"))
        if path.suffix in _SUFFIXES
        and path.is_file()
        and "tests" not in path.relative_to(ROLES).parts
        for live in _shebang_runs(role_installs.command_text(path), installed)
    ]
    assert not offenders, (
        "these run an installed role script by path, so its shebang picks the host's system "
        "Python. Run it through `/usr/local/bin/uv run --no-project --no-python-downloads "
        "--python {{ host_python_version }} <path>`:\n  " + "\n  ".join(offenders)
    )


def test_a_script_run_by_path_is_flagged():
    text = 'done < <(/usr/local/bin/x.py "$ROOT" 2>/dev/null)'
    assert _shebang_runs(text, ["/usr/local/bin/x.py"]) == ["/usr/local/bin/x.py"]


def test_a_script_run_through_uv_is_clean():
    folded = (
        "    cmd: >-\n"
        "      /usr/local/bin/uv run --no-project --no-python-downloads --python 3.14.6\n"
        "      /usr/local/bin/x.py\n"
    )
    assert _shebang_runs(folded, ["/usr/local/bin/x.py"]) == []
