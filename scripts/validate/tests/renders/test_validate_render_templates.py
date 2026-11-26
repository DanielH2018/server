"""Tests for scripts/validate/render_templates.py, the entry point over the render families.

Each family's own rules, and the known-bad fixture each must flag, are in
`test_render_<family>.py` beside this file. This module runs every family over the real tree
and holds the entry point's summary and exit code.
"""

import re
import shutil
import tomllib
from collections.abc import Callable

import pytest
from lib.proc_testing import run
from lib.repo_paths import REPO
from validate import render_templates as rt


def _clean(ok: Callable[[str], None]) -> tuple[int, int]:
    ok("  [ok]   x")
    return 3, 0


def _broken(ok: Callable[[str], None]) -> tuple[int, int]:
    return 2, 1


def _family(run, files: str = ".*") -> rt.Family:
    return rt.Family(run, re.compile(files))


def _chosen(path: str) -> set[str]:
    return {name for name, family in rt.FAMILIES.items() if family.files.match(path)}


FAMILIES = ("compose", "k8s", "setup", "shell", "units")


def test_the_entry_point_runs_every_family():
    assert set(rt.FAMILIES) == set(FAMILIES)


# The k8s family's real-tree run is in `test_render_k8s.py`, off the run's shared k8s render.
@pytest.mark.parametrize("name", [name for name in FAMILIES if name != "k8s"])
def test_each_family_checks_the_real_tree_clean(name):
    if name == "units" and shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not on PATH")
    checked, failures = rt.FAMILIES[name].run(print)
    assert checked > 0
    assert failures == 0


def test_a_family_failure_fails_the_run_and_is_named_in_the_summary(capsys):
    assert rt.main([], {"clean": _family(_clean), "broken": _family(_broken)}) == 1
    assert (
        capsys.readouterr()
        .out.strip()
        .endswith("5 template(s) checked (clean 3, broken 2 (1 failed)), 1 failure(s).")
    )


def test_clean_families_pass_and_print_no_ok_line_without_verbose(capsys):
    assert rt.main([], {"clean": _family(_clean)}) == 0
    assert "[ok]" not in capsys.readouterr().out


def test_verbose_passes_the_ok_lines_through(capsys):
    assert rt.main(["-v"], {"clean": _family(_clean)}) == 0
    assert "  [ok]   x\n" in capsys.readouterr().out


def _hook_pattern() -> re.Pattern[str]:
    hooks = [
        hook
        for repo in tomllib.loads((REPO / "prek.toml").read_text())["repos"]
        for hook in repo.get("hooks", [])
        if hook["id"] == "validate-render-templates"
    ]
    return re.compile(hooks[0]["files"])


def _tracked() -> list[str]:
    return run(["git", "ls-files"], cwd=REPO, check=True).stdout.splitlines()


def test_every_path_the_hook_matches_selects_a_family():
    pattern = _hook_pattern()
    matched = [path for path in _tracked() if pattern.search(path)]
    assert "hosts/components/pi-compose/services/alloy/compose.yaml.j2" in matched
    assert [path for path in matched if not _chosen(path)] == []


def test_the_hook_matches_every_path_a_family_selects():
    pattern = _hook_pattern()
    selected = [path for path in _tracked() if _chosen(path)]
    assert "k8s/roles/longhorn/defaults/main.yml" in selected
    assert [path for path in selected if not pattern.search(path)] == []


def test_a_compose_template_selects_compose_and_the_host_render_only():
    assert _chosen("hosts/components/pi-compose/services/alloy/compose.yaml.j2") == {
        "compose",
        "units",
    }


def test_a_role_shell_template_selects_shell_and_the_k8s_render_that_can_embed_it():
    assert _chosen("k8s/roles/longhorn/files/x.sh.j2") == {"shell", "k8s"}


def test_a_role_manifest_template_selects_the_k8s_family_alone():
    assert _chosen("k8s/roles/traefik/templates/deployment.yaml.j2") == {"k8s"}


def test_a_role_s_defaults_select_each_family_that_renders_over_them():
    assert _chosen("k8s/roles/longhorn/defaults/main.yml") == {"shell", "units", "k8s"}


def test_a_family_the_environment_names_is_skipped_and_the_summary_says_so(capsys):
    table = {"clean": _family(_clean), "broken": _family(_broken)}
    assert rt.main([], table, {rt.SKIP_ENV: "broken"}) == 0
    assert (
        capsys.readouterr()
        .out.strip()
        .endswith(
            "3 template(s) checked (clean 3), 0 failure(s); skipped broken, RENDER_TEMPLATES_SKIP."
        )
    )


def test_a_skip_naming_no_family_is_flagged(capsys):
    assert rt.main([], {"clean": _family(_clean)}, {rt.SKIP_ENV: "k8s-manifests"}) == 1
    assert "names no family: k8s-manifests" in capsys.readouterr().err


def test_a_shared_input_selects_every_family():
    assert _chosen("scripts/lib/render_guard.py") == set(FAMILIES)


def test_files_run_only_the_matching_families_and_the_summary_names_the_skipped(capsys):
    table = {"clean": _family(_clean, "a/.*"), "broken": _family(_broken, "b/.*")}
    assert rt.main(["a/x.j2"], table) == 0
    assert (
        capsys.readouterr()
        .out.strip()
        .endswith(
            "3 template(s) checked (clean 3), 0 failure(s); skipped broken, no input changed."
        )
    )
