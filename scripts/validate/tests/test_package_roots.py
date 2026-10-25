"""The `host_packages` job's two scans over the named package roots (H4.1c).

`gitleaks dir` and the placeholder check read the head render's `homelab-updater`,
`homelab-daniel-pi`, `homelab-daniel-server` and `homelab-daniel-box` roots. The gitleaks binary
exists only inside the job, so these cases drive the scan with a stub that honours the contract the job relies on, and the job's `prove-scan`
step proves the real binary honours it too.

Run: uv run pytest scripts/validate/tests/test_package_roots.py
"""

import sys
from pathlib import Path

import pytest
from lib import proc_testing
from lib.repo_paths import REPO
from validate import host_packages
from validate.validate_lib import package_roots

SOPS_DIR = package_roots.SOPS_DIR
TEMPLATES = package_roots.TEMPLATES
COMPOSE = package_roots.COMPOSE
# A gitleaks stand-in: it flags a file holding the marker anywhere except the allowlisted
# ciphertext path, relative to its working directory, as the real binary does with `dir .`.
STUB = f"""#!{sys.executable}
import pathlib, sys
if sys.argv[1:2] != ["dir"] or sys.argv[2] != ".":
    sys.exit(2)
hits = [
    p for p in pathlib.Path().rglob("*")
    if p.is_file() and ("LEAK" in p.read_text() or "ghp_" in p.read_text())
    and not (p.parent.as_posix() == "{SOPS_DIR}" and p.name.endswith(".sops.yaml"))
]
sys.exit(1 if hits else 0)
"""


def _package(render: Path, name: str, directory: str) -> Path:
    """A rendered package `name` under `render`; return its root."""
    package = render / directory
    package.mkdir(parents=True)
    (package / "nfpm.yaml").write_text(f"name: {name}\n")
    (package / "root").mkdir()
    return package / "root"


def _render(tmp_path: Path) -> dict[str, Path]:
    """A render holding every named package, with empty roots."""
    return {
        "homelab-updater": _package(tmp_path, "homelab-updater", "packages/u"),
        "homelab-daniel-pi": _package(tmp_path, "homelab-daniel-pi", "hosts/daniel-pi"),
        "homelab-daniel-server": _package(
            tmp_path, "homelab-daniel-server", "hosts/daniel-server"
        ),
        "homelab-daniel-box": _package(
            tmp_path, "homelab-daniel-box", "hosts/daniel-box"
        ),
    }


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _stub(tmp_path: Path) -> Path:
    stub = tmp_path / "leakscan"
    proc_testing.write_exec(stub, STUB)
    return stub


def test_the_real_render_has_every_named_root_and_no_bad_placeholder(tmp_path):
    assert host_packages.render(REPO, tmp_path) == 0
    roots, problems = package_roots.package_roots(tmp_path)
    # Named members: a lookup that stopped finding a package would scan less and pass.
    assert sorted(roots) == sorted(package_roots.NAMED)
    assert problems == []
    assert package_roots.check_placeholders(tmp_path) == []


def test_a_missing_named_package_is_flagged_by_name(tmp_path):
    roots = _render(tmp_path)
    (roots["homelab-daniel-pi"].parent / "nfpm.yaml").write_text("name: renamed\n")
    problems = package_roots.check_placeholders(tmp_path)
    assert len(problems) == 1 and problems[0].startswith("[FAIL] homelab-daniel-pi:")


def test_a_named_package_without_a_root_is_flagged(tmp_path):
    roots = _render(tmp_path)
    roots["homelab-updater"].rmdir()
    (problem,) = package_roots.package_roots(tmp_path)[1]
    assert "homelab-updater" in problem and "not a directory" in problem


def test_a_placeholder_the_sops_file_carries_is_clean(tmp_path):
    root = _render(tmp_path)["homelab-daniel-pi"]
    _write(root, f"{SOPS_DIR}/daniel-pi.sops.yaml", "alpha: ENC[x]\nsops: {}\n")
    _write(root, f"{TEMPLATES}/etc/app.conf", "pw=${alpha} lit=$${beta} cost=$$5\n")
    _write(root, f"{COMPOSE}/c.yml", "environment:\n  PW: ${alpha}\n")
    assert package_roots.placeholder_problems(root) == []


def test_a_placeholder_no_key_carries_is_flagged(tmp_path):
    root = _render(tmp_path)["homelab-daniel-pi"]
    _write(root, f"{SOPS_DIR}/daniel-pi.sops.yaml", "alpha: ENC[x]\nsops: {}\n")
    _write(root, f"{TEMPLATES}/etc/app.conf", "pw=${alpha}\nbad=${no_such_key}\n")
    (problem,) = package_roots.placeholder_problems(root)
    assert "etc/app.conf:2" in problem and "${no_such_key}" in problem


def test_a_compose_placeholder_without_a_sops_file_is_flagged(tmp_path):
    root = _render(tmp_path)["homelab-updater"]
    _write(root, f"{COMPOSE}/c.yml", "PW: ${alpha}\n")
    (problem,) = package_roots.placeholder_problems(root)
    assert "c.yml:1" in problem


def test_a_template_with_an_unclosed_brace_is_flagged(tmp_path):
    root = _render(tmp_path)["homelab-updater"]
    _write(root, f"{TEMPLATES}/etc/app.conf", "x=${oops\n")
    (problem,) = package_roots.placeholder_problems(root)
    assert "does not close" in problem


def test_the_scan_passes_roots_the_binary_passes_and_names_one_it_flags(tmp_path):
    roots = _render(tmp_path)
    stub = _stub(tmp_path)
    assert package_roots.scan(tmp_path, stub, REPO / ".gitleaks.toml") == []
    _write(roots["homelab-daniel-pi"], "etc/x.conf", "LEAK\n")
    assert package_roots.scan(tmp_path, stub, REPO / ".gitleaks.toml") == [
        "[FAIL] homelab-daniel-pi: gitleaks exited 1"
    ]


def test_the_scan_fails_on_a_missing_root_without_running_the_binary(tmp_path):
    stub = _stub(tmp_path)
    problems = package_roots.scan(tmp_path / "empty", stub, REPO / ".gitleaks.toml")
    assert len(problems) == len(package_roots.NAMED)


def test_the_proof_passes_a_binary_that_honours_the_allowlist_path(tmp_path):
    assert package_roots.prove(_stub(tmp_path), REPO / ".gitleaks.toml") == []


@pytest.mark.parametrize(
    ("exit_code", "where"),
    [(0, "etc/x.conf"), (1, f"{SOPS_DIR}/x.sops.yaml")],
)
def test_the_proof_flags_a_binary_that_ignores_the_path_rule(
    tmp_path, exit_code, where
):
    """A binary that flags nothing, or flags the ciphertext path too, fails the proof."""
    stub = tmp_path / "leakscan"
    proc_testing.write_exec(stub, f"#!/bin/sh\nexit {exit_code}\n")
    problems = package_roots.prove(stub, REPO / ".gitleaks.toml")
    assert len(problems) == 1 and where in problems[0]
