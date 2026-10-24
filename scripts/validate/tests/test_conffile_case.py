"""The line arm's two owning-package upgrade cases, without a container (#6088).

H7's *Red proofs*, line arm, names two upgrades of the package that owns a conffile the arm
edits. In the first, the conffile is unchanged and the line stays. In the second, the upgrade
ships a changed conffile and runs non-interactively under `--force-confold`, as
unattended-upgrades runs it. dpkg then either keeps the local file or holds the package back.
The case records which, and asserts the line is present after the next reconcile.

The cases replace a conffile with real dpkg, so they run only in CI's `host_packages` job, from
`validate_lib/conffile_case.py`, as `secrets_case.py` runs the secrets-unit case. These tests
cover what needs no container:

  * the owning fixture's builds, which `owner_debs` writes with `dpkg-deb`, since the pytest
    job has no nfpm;
  * the homelab fixture that declares a line-arm edit on the conffile;
  * the classifier that names dpkg's outcome;
  * where the job runs the cases, and how the upgrade and reconcile are invoked.

The module does not exist until the fix adds it, so every test reaches it in its body.

Run: uv run pytest scripts/validate/tests/test_conffile_case.py
"""

import ast
import importlib
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType

import pytest
from lib.proc_testing import DEFAULT_TIMEOUT
from validate.validate_lib import host_fixtures as fx
from validate.validate_lib.host_container import CaseFailed

MODULE = "validate.validate_lib.conffile_case"
SOURCE = "scripts/validate/validate_lib/conffile_case.py"
CASES = "scripts/validate/host_package_cases.py"
# The functions the fix adds to the module, each reached through getattr.
OWNER_DEBS = "owner_debs"
OUTCOME = "outcome"
KEPT = "kept"
HELD_BACK = "held back"


def _module() -> ModuleType:
    assert importlib.util.find_spec(MODULE) is not None, (
        f"{SOURCE} does not exist, so the job runs no owning-package upgrade case"
    )
    return importlib.import_module(MODULE)


def _function(name: str):
    module = _module()
    assert hasattr(module, name), f"{SOURCE} defines no {name}"
    return getattr(module, name)


def _conffiles(deb: Path) -> list[str]:
    """The paths a deb's `conffiles` control member lists, read by dpkg-deb."""
    done = subprocess.run(
        ["dpkg-deb", "--ctrl-tarfile", str(deb)],
        capture_output=True,
        check=True,
        timeout=DEFAULT_TIMEOUT,
    )
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        members = {member.name.lstrip("./"): member for member in tar.getmembers()}
        if "conffiles" not in members:
            return []
        member = tar.extractfile(members["conffiles"])
        assert member is not None
        return [line for line in member.read().decode().splitlines() if line.strip()]


def _shipped(deb: Path, path: str) -> bytes:
    """The bytes a deb ships at `path`, read from dpkg-deb's filesystem tarball."""
    done = subprocess.run(
        ["dpkg-deb", "--fsys-tarfile", str(deb)],
        capture_output=True,
        check=True,
        timeout=DEFAULT_TIMEOUT,
    )
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        for member in tar.getmembers():
            if "/" + member.name.lstrip("./") == path:
                extracted = tar.extractfile(member)
                assert extracted is not None
                return extracted.read()
    raise AssertionError(f"{deb.name} ships nothing at {path}")


def _version(deb: Path) -> str:
    return fx.deb_version(deb)


def _owner_builds(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """The owning fixture's base, unchanged and changed builds, and the conffile they share.

    The roles come from the builds themselves: the base is the oldest, the unchanged build ships
    the base's conffile bytes, and the changed build ships other bytes.
    """
    debs = list(_function(OWNER_DEBS)(tmp_path / "owner"))
    assert len(debs) == 3, (
        f"{OWNER_DEBS} built {len(debs)} debs, not base and two upgrades"
    )
    oldest = debs[0]
    for deb in debs[1:]:
        if fx.newer(_version(oldest), _version(deb)):
            oldest = deb
    upgrades = [deb for deb in debs if deb != oldest]
    (conffile,) = _conffiles(oldest)
    base = _shipped(oldest, conffile)
    same = [deb for deb in upgrades if _shipped(deb, conffile) == base]
    other = [deb for deb in upgrades if _shipped(deb, conffile) != base]
    assert len(same) == 1 and len(other) == 1, (
        "the two upgrades do not split into one unchanged and one changed conffile"
    )
    return oldest, same[0], other[0], conffile


def _declared_edits(tmp_path: Path) -> list[dict]:
    """Every line-arm edit a rendered fixture variant's manifest declares."""
    edits = []
    for variant in fx.VARIANTS:
        out = fx.render_fixture(variant, tmp_path / variant, payload_bytes=16)
        manifest = json.loads((out / "root/usr/lib/homelab/manifest.json").read_text())
        edits += manifest.get("lines", {}).get("edits", [])
    return edits


# The owning fixture.


def test_the_owning_fixture_lists_one_conffile_in_every_build(tmp_path):
    debs = list(_function(OWNER_DEBS)(tmp_path / "owner"))
    assert debs
    listed = [_conffiles(deb) for deb in debs]
    assert all(len(paths) == 1 for paths in listed), listed
    assert len({paths[0] for paths in listed}) == 1, listed


def test_the_owning_fixture_has_an_unchanged_and_a_changed_upgrade_both_newer(tmp_path):
    base, unchanged, changed, _ = _owner_builds(tmp_path)
    assert fx.newer(_version(unchanged), _version(base))
    assert fx.newer(_version(changed), _version(base))


# The homelab fixture that edits it.


def test_a_homelab_fixture_declares_a_line_edit_on_the_owning_fixtures_conffile(
    tmp_path,
):
    base, unchanged, changed, conffile = _owner_builds(tmp_path)

    edits = [edit for edit in _declared_edits(tmp_path) if edit["path"] == conffile]

    assert edits, f"no fixture variant declares a line-arm edit on {conffile}"
    for edit in edits:
        text = (edit.get("line") or edit.get("block") or "").encode()
        assert text
        # A line the package already ships would survive any upgrade, proving nothing.
        for deb in (base, unchanged, changed):
            assert text not in _shipped(deb, conffile), (
                f"{deb.name} already ships the declared edit"
            )


# What the case records.


# `outcome(before, offered, after, line_kept)` takes the owner's installed version before the
# upgrade, the version the repository offered, the installed version after it, and whether the
# arm's line was still in the conffile after the upgrade and before the next reconcile.


def test_an_upgrade_dpkg_installed_with_the_local_file_kept_is_recorded_as_kept():
    outcome = _function(OUTCOME)
    assert outcome("0.1", "0.2", "0.2", True) == KEPT


def test_an_upgrade_dpkg_left_at_the_old_version_is_recorded_as_held_back():
    outcome = _function(OUTCOME)
    assert outcome("0.1", "0.2", "0.1", True) == HELD_BACK


def test_an_upgrade_that_replaced_the_local_file_fails_the_case():
    # Neither outcome --force-confold allows. The next reconcile re-adds the line, so only
    # this check sees it.
    outcome = _function(OUTCOME)
    with pytest.raises(CaseFailed):
        outcome("0.1", "0.2", "0.2", False)


@pytest.mark.parametrize("after", ["", "0.3"])
def test_an_installed_version_that_is_neither_fails_the_case(after):
    outcome = _function(OUTCOME)
    with pytest.raises(CaseFailed):
        outcome("0.1", "0.2", after, True)


# Where and how the job runs the cases.


def _main_runs_the_conffile_case(source: str) -> bool:
    """Whether `main` in the cases' source calls `conffile_case.run(...)`."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "run"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "conffile_case"
                for call in ast.walk(node)
            )
    return False


def test_the_host_packages_job_runs_the_conffile_case():
    source = (fx.REPO / CASES).read_text()
    assert _main_runs_the_conffile_case(source)
    # Red proof: the secrets case's run, which main calls already, does not count.
    assert not _main_runs_the_conffile_case(
        source.replace("conffile_case.run", "secrets_case.run")
    )


def _docstrings(node: ast.AST) -> set[int]:
    """The ids of the docstring constants under `node`, which say nothing about what runs."""
    found = set()
    for each in ast.walk(node):
        body = getattr(each, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
        ):
            found.add(id(body[0].value))
    return found


def _strings(node: ast.AST) -> list[tuple[int, str]]:
    """Each string constant under `node` but its docstrings, an f-string's parts joined."""
    found = []
    skip = _docstrings(node)
    for each in ast.walk(node):
        if id(each) in skip:
            continue
        if isinstance(each, ast.JoinedStr):
            text = "".join(
                part.value
                for part in each.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            found.append((each.lineno, text))
        elif isinstance(each, ast.Constant) and isinstance(each.value, str):
            found.append((each.lineno, each.value))
    return found


def _upgrades_then_reconciles(source: str) -> list[str]:
    """The functions that upgrade non-interactively with --force-confold, then reconcile.

    The upgrade is a string carrying `--force-confold` in a module that also names
    `noninteractive` (a `DEBIAN_FRONTEND`, perhaps in a constant the command interpolates) or
    `unattended-upgrade` (the tool itself). The reconcile is a later string, in the same
    function, naming `homelab-reconcile`.
    """
    found = []
    tree = ast.parse(source)
    unattended = any(
        "noninteractive" in text or "unattended-upgrade" in text
        for _, text in _strings(tree)
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        strings = _strings(node)
        upgrades = [line for line, text in strings if "--force-confold" in text]
        if (
            upgrades
            and unattended
            and any(
                line > min(upgrades) and "homelab-reconcile" in text
                for line, text in strings
            )
        ):
            found.append(node.name)
    return found


def test_the_case_upgrades_non_interactively_under_force_confold_then_reconciles():
    _module()
    source = (fx.REPO / SOURCE).read_text()
    assert _upgrades_then_reconciles(source)
    # Red proofs: an interactive upgrade, and an upgrade with no reconcile after it, do not count.
    interactive = source.replace("noninteractive", "readline")
    assert not _upgrades_then_reconciles(interactive.replace("unattended-upgrade", "x"))
    assert not _upgrades_then_reconciles(source.replace("homelab-reconcile", "nothing"))


def _confold_commands(source: str) -> list[tuple[str, str]]:
    """Each string but a docstring that carries `--force-confold`, with its source text.

    Adjacent literals are one node, so a command split over two lines is one string here.
    """
    found = []
    tree = ast.parse(source)
    skip = _docstrings(tree)
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.JoinedStr | ast.Constant):
            text = (
                "".join(
                    part.value
                    for part in node.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
                if isinstance(node, ast.JoinedStr)
                else node.value
            )
            if isinstance(text, str) and "--force-confold" in text:
                found.append((text, ast.get_source_segment(source, node) or ""))
    return found


def test_the_force_confold_command_itself_runs_without_a_terminal():
    # The module-wide check above passes while another command carries the frontend, so this
    # one reads the upgrade command alone.
    _module()
    source = (fx.REPO / SOURCE).read_text()
    commands = _confold_commands(source)
    assert commands
    assert all("DEBIAN_FRONTEND=noninteractive" in text for text, _ in commands)
    # Red proof: the frontend dropped from the upgrade command only, kept everywhere else.
    _, segment = commands[0]
    interactive = source.replace(
        segment, segment.replace("DEBIAN_FRONTEND=noninteractive ", "")
    )
    assert "DEBIAN_FRONTEND=noninteractive" in interactive
    assert not all(
        "DEBIAN_FRONTEND=noninteractive" in text
        for text, _ in _confold_commands(interactive)
    )
