"""The deploy plane and the tests a fan-out brief precomputes from an issue's cited paths (#3955).

The deploy line runs `land.sh`'s own classifier against this checkout, so its cases name real
roles. The test mapping runs against a scratch tree.
"""

from _findings_fakes import Fakes, build_tools, make_issue
from fanout_lib.brief import Issue, render_brief
from fanout_lib.target import Target

from dev.findings import main
from dev.findings_lib.precomputed import deploy_line, covering_tests

SONARR = "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"
TOOLING = "scripts/dev/fanout_lib/brief.py"


def test_a_k8s_role_template_deploys_its_tag():
    assert deploy_line([SONARR]).startswith("deploys tag sonarr")


def test_a_scripts_only_change_is_expected_to_deploy_nothing():
    assert deploy_line([TOOLING]) == "nothing to deploy expected"


def _tree(tmp_path, files):
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


def test_a_module_maps_to_the_tests_that_name_or_import_it(tmp_path):
    repo = _tree(
        tmp_path,
        {
            "s/pkg_lib/mod.py": "",
            "s/tests/test_pkg_mod.py": "",
            "s/tests/test_other.py": "from pkg_lib.mod import f\n",
            "s/tests/test_unrelated.py": "import os\n",
            "r/files/app.py": "",
            "r/tests/test_app_cli.py": "",
        },
    )
    assert covering_tests(
        ["s/pkg_lib/mod.py", "r/files/app.py", "docs/x.md"], repo
    ) == [
        "r/tests/test_app_cli.py",
        "s/tests/test_other.py",
        "s/tests/test_pkg_mod.py",
    ]


def test_a_module_no_test_names_or_imports_maps_to_nothing(tmp_path):
    repo = _tree(tmp_path, {"s/mod.py": "", "s/tests/test_other.py": "import os\n"})
    assert covering_tests(["s/mod.py"], repo) == []


def test_the_brief_carries_the_block_for_this_repo_only():
    issue = Issue(1, "t", f"Fix `{SONARR}`.")
    brief = render_brief([issue], "daniel-box", "1", "worktree-orch", [])
    assert "## Precomputed from the cited paths" in brief
    assert "deploys tag sonarr" in brief
    dotfiles = Target("DanielH2018/dotfiles", "/tmp/dotfiles", "origin/main")
    other = render_brief([issue], "daniel-box", "1", "w", [], target=dotfiles)
    assert "## Precomputed" not in other


def test_show_brief_prints_only_the_block(capsys):
    issue = make_issue(5)
    issue["body"] = f"`{SONARR}` has no probe."
    tools, _ = build_tools(Fakes(view=issue))
    assert main(["show", "5", "--brief"], tools) == 0
    out = capsys.readouterr().out
    assert out.startswith("## Precomputed from the cited paths")
    assert "deploys tag sonarr" in out
