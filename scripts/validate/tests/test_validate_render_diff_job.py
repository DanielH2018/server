"""The render-diff job's verdict on fixture trees, and its command line in a scratch repo.

The workflow that calls it runs only on GitHub. Each verdict case here is one the workflow's
red-proofs show on a real render.

Run: uv run pytest scripts/validate/tests/test_validate_render_diff_job.py
"""

import shutil
from pathlib import Path

import pytest

from lib.git_testing import commit, init_repo, scrub_process_git_env
from lib.proc_testing import fake_bin, path_with
from renderer.renderer_lib.bundle import SHARED_KINDS
from renderer.renderer_lib.cluster import flux_owned_tags
from validate import render_diff_job as job

PIHOLE = "flux/pihole.yaml"
TRAEFIK = "flux/traefik.yaml"
RENDERER_CHANGE = [("M", "scripts/renderer/renderer_lib/soak.py")]
DOCS_CHANGE = [("M", "docs/homelab-render.md")]


def tree(root: Path, overrides: dict[str, str] | None = None) -> Path:
    """A rendered tree that clears a floor of 2, with `overrides` written over its files."""
    files = {
        PIHOLE: "pihole\n",
        TRAEFIK: "traefik\n",
        "cluster/pihole-deferred/sync.yaml": "deferred\n",
        job.DERIVED: "{}\n",
        **(overrides or {}),
    }
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)
    return root


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tree(tmp_path / "base")


def head(tmp_path: Path, overrides: dict[str, str] | None = None) -> Path:
    return tree(tmp_path / "head", overrides)


def verdict(base: Path, new: Path, changes, body: str) -> job.Verdict:
    return job.judge(base, new, changes, body, floor=2)


def test_equal_trees_pass_under_none_on_a_hazard_path(base, tmp_path):
    outcome = verdict(base, head(tmp_path), RENDERER_CHANGE, "Render-diff: none")
    assert outcome.problems == []


def test_a_field_edit_under_none_fails_and_names_the_file(base, tmp_path):
    moved = head(tmp_path, {PIHOLE: "pihole: edited\n"})
    assert verdict(base, moved, RENDERER_CHANGE, "Render-diff: none").problems == [
        f"Render-diff: none, yet {PIHOLE} differs"
    ]


def test_the_field_edit_passes_once_the_line_names_the_file(base, tmp_path):
    moved = head(
        tmp_path, {PIHOLE: "pihole: edited\n", job.DERIVED: '{"moved": true}\n'}
    )
    assert (
        verdict(base, moved, RENDERER_CHANGE, f"Render-diff: {PIHOLE}").problems == []
    )


def test_a_derived_digest_change_alone_still_fails_under_none(base, tmp_path):
    moved = head(tmp_path, {job.DERIVED: '{"moved": true}\n'})
    problems = verdict(base, moved, RENDERER_CHANGE, "Render-diff: none").problems
    assert problems == [f"Render-diff: none, yet {job.DERIVED} differs"]


def test_a_listed_path_that_did_not_move_fails(base, tmp_path):
    problems = verdict(
        base, head(tmp_path), RENDERER_CHANGE, f"Render-diff: {PIHOLE}"
    ).problems
    assert problems == [f"the Render-diff: line names {PIHOLE}, which does not differ"]


def test_an_unlisted_file_fails_beside_a_listed_one(base, tmp_path):
    moved = head(tmp_path, {PIHOLE: "edited\n", TRAEFIK: "edited\n"})
    assert verdict(base, moved, DOCS_CHANGE, f"Render-diff: {PIHOLE}").problems == [
        "flux/traefik.yaml differs, and the Render-diff: line omits it"
    ]


def test_a_tag_that_appears_must_be_listed(base, tmp_path):
    moved = head(tmp_path, {"flux/new.yaml": "new\n"})
    assert (
        "flux/new.yaml appears or disappears, and the Render-diff: line omits it"
        in (verdict(base, moved, DOCS_CHANGE, f"Render-diff: {PIHOLE}").problems)
    )


@pytest.mark.parametrize(
    "change",
    [
        ("M", "scripts/renderer/homelab_render.py"),
        ("M", "scripts/validate/render_diff_job.py"),
        ("M", "scripts/lib/k8s_roles.py"),
        ("M", "scripts/lib/ansible_jinja_env.py"),
        ("M", "ansible/filter_plugins/toposort.py"),
        ("M", "ansible/roles/k8s/flux/tasks/main.yml"),
        ("D", "ansible/roles/k8s/flux/defaults/main.yml"),
    ],
)
def test_a_hazard_path_with_no_line_fails_and_names_both_forms(base, tmp_path, change):
    problems = verdict(base, head(tmp_path), [change], "no line here").problems
    assert len(problems) == 1
    assert change[1] in problems[0]
    assert "Render-diff: none" in problems[0] and "listing every file" in problems[0]


@pytest.mark.parametrize(
    "change",
    [
        ("M", "ansible/roles/k8s/flux/defaults/main.yml"),
        ("D", "ansible/roles/setup/gitops_deploy/defaults/main.yml"),
        ("M", "docs/homelab-render.md"),
    ],
)
def test_no_hazard_and_no_line_prints_the_difference_and_passes(base, tmp_path, change):
    moved = head(tmp_path, {PIHOLE: "image bump\n"})
    outcome = verdict(base, moved, [change], "no line here")
    assert outcome.problems == []
    assert outcome.differences == {PIHOLE}


def test_two_empty_renders_fail_the_floor(tmp_path):
    empty = tmp_path / "base"
    empty.mkdir()
    problems = job.judge(
        empty, empty, DOCS_CHANGE, "Render-diff: none", floor=2
    ).problems
    for side in ("base", "head"):
        assert f"{side}: flux/pihole.yaml is missing from the render" in problems
        assert any(p.startswith(f"{side}: flux/ holds 0 files") for p in problems)


def test_a_listed_retired_tag_lowers_the_heads_floor_alone(tmp_path):
    old = tree(tmp_path / "base", {"flux/gone.yaml": "gone\n"})
    new = head(tmp_path)
    listed = "Render-diff: flux/gone.yaml"
    assert job.judge(old, new, DOCS_CHANGE, listed, floor=3).problems == []
    assert job.judge(old, new, DOCS_CHANGE, "no line", floor=3).problems != []
    assert job.judge(new, new, DOCS_CHANGE, "no line", floor=3).problems != []


def test_the_floor_never_exceeds_what_the_tree_publishes():
    published = len(flux_owned_tags()) + len(SHARED_KINDS) + 1
    assert job.FLUX_FLOOR <= published


def test_a_removed_deferred_directory_fails_the_floor(base, tmp_path):
    moved = head(tmp_path)
    shutil.rmtree(moved / "cluster/pihole-deferred")
    problems = verdict(
        base, moved, DOCS_CHANGE, "Render-diff: cluster/pihole-deferred/sync.yaml"
    )
    assert problems.problems == [
        "head: cluster/pihole-deferred is missing from the render"
    ]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Render-diff: none", frozenset()),
        ("text\r\nRender-diff: none\r\nmore", frozenset()),
        ("Render-diff: a.yaml, b.yaml", frozenset({"a.yaml", "b.yaml"})),
        ("Render-diff: a.yaml b.yaml", frozenset({"a.yaml", "b.yaml"})),
        ("Render-diff: `a.yaml`, b.yaml,", frozenset({"a.yaml", "b.yaml"})),
        ("Render-diff: `none`", frozenset()),
        ("Render-diff: none\nRender-diff: none", frozenset()),
        ("no line", None),
        ("```bash\nRender-diff: none\n```", None),
        ("<!--\nRender-diff: none\n-->", None),
        ("`Render-diff: none` in prose", None),
    ],
)
def test_the_line_binds_only_outside_a_fence_or_a_comment(body, expected):
    assert job.declared(body) == expected


@pytest.mark.parametrize(
    "body", ["Render-diff:", "Render-diff: none\nRender-diff: a.yaml"]
)
def test_a_malformed_line_fails_the_job(base, tmp_path, body):
    problems = verdict(base, head(tmp_path), DOCS_CHANGE, body).problems
    assert len(problems) == 1 and "Render-diff: none" in problems[0]


def test_both_sides_render_the_publish_jobs_form_with_the_pinned_commit():
    command = job.render_command(["pihole", "traefik"], Path("/out"), "a" * 40)
    assert command[command.index("--revision") + 1] == "a" * 40
    assert "--all-images" in command and "--shared-only" not in command
    assert command[-4:] == ["--role", "pihole", "--role", "traefik"]


def test_no_published_tag_renders_the_shared_kinds_alone():
    command = job.render_command([], Path("/out"), "a" * 40)
    assert "--shared-only" in command and "--role" not in command


MOVED_DEFAULT = "ansible/roles/k8s/demo/defaults/main.yml"


@pytest.fixture
def renamed_out_of_a_role(tmp_path, monkeypatch) -> tuple[str, str]:
    """A scratch repo as the cwd, whose second commit renames a file out of a k8s role."""
    scrub_process_git_env(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    first = commit(repo, "add", **{MOVED_DEFAULT: "key: value\n"})
    second = commit(
        repo, "rename", **{MOVED_DEFAULT: None, "docs/moved.yml": "key: value\n"}
    )
    monkeypatch.chdir(repo)
    return first, second


def full_tree(root: Path) -> Path:
    """A rendered tree that clears the real FLUX_FLOOR."""
    tree(root)
    for number in range(job.FLUX_FLOOR):
        (root / "flux" / f"tag-{number}.yaml").write_text("tag\n")
    return root


def test_a_rename_splits_into_its_delete_and_its_add(renamed_out_of_a_role):
    assert sorted(job.changed_paths(*renamed_out_of_a_role)) == [
        ("A", "docs/moved.yml"),
        ("D", MOVED_DEFAULT),
    ]


def compare_args(tmp_path: Path, shas: tuple[str, str]) -> list[str]:
    trees = [str(full_tree(tmp_path / side)) for side in ("out-base", "out-head")]
    return ["compare", *trees, "--merge-base", shas[0], "--head", shas[1]]


def test_main_refuses_a_rename_out_of_a_role_with_no_line(
    tmp_path, monkeypatch, capsys, renamed_out_of_a_role
):
    monkeypatch.setenv("PR_BODY", "no line here")
    assert job.main(compare_args(tmp_path, renamed_out_of_a_role)) == 1
    assert f"the first {MOVED_DEFAULT}" in capsys.readouterr().out


def test_main_passes_the_same_rename_once_the_body_carries_the_line(
    tmp_path, monkeypatch, renamed_out_of_a_role
):
    monkeypatch.setenv("PR_BODY", "Render-diff: none")
    assert job.main(compare_args(tmp_path, renamed_out_of_a_role)) == 0


def _lock(jinja: str = "3.1.6", tool: str = "1.0") -> str:
    return (
        f'[[package]]\nname = "jinja2"\nversion = "{jinja}"\n\n'
        f'[[package]]\nname = "pytest-tool"\nversion = "{tool}"\n'
    )


def _project(requirement: str = "jinja2>=3") -> str:
    return f'[dependency-groups]\ndev = ["{requirement}"]\n'


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    """Commits a base and a head of a scratch repo's two pin files, and names the two SHAs."""
    scrub_process_git_env(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    def commits(head_lock: str, head_project: str = _project()) -> tuple[str, str]:
        base = commit(
            repo, "base", **{"uv.lock": _lock(), "pyproject.toml": _project()}
        )
        return base, commit(
            repo, "head", **{"uv.lock": head_lock, "pyproject.toml": head_project}
        )

    return commits


def test_main_refuses_a_jinja2_bump_with_no_line(tmp_path, monkeypatch, capsys, pinned):
    monkeypatch.setenv("PR_BODY", "no line here")
    assert job.main(compare_args(tmp_path, pinned(_lock(jinja="3.1.7")))) == 1
    assert "entry for jinja2" in capsys.readouterr().out


def test_main_passes_a_jinja2_bump_once_the_body_carries_the_line(
    tmp_path, monkeypatch, pinned
):
    monkeypatch.setenv("PR_BODY", "Render-diff: none")
    assert job.main(compare_args(tmp_path, pinned(_lock(jinja="3.1.7")))) == 0


def test_main_refuses_a_requirement_edit_with_no_line(
    tmp_path, monkeypatch, capsys, pinned
):
    monkeypatch.setenv("PR_BODY", "no line here")
    shas = pinned(_lock(), _project("jinja2>=4"))
    assert job.main(compare_args(tmp_path, shas)) == 1
    assert "entry for jinja2" in capsys.readouterr().out


def test_main_passes_an_unrelated_dependency_bump_with_no_line(
    tmp_path, monkeypatch, pinned
):
    monkeypatch.setenv("PR_BODY", "no line here")
    assert job.main(compare_args(tmp_path, pinned(_lock(tool="2.0")))) == 0


FAKE_UV = """\
#!/bin/sh
echo "$@" >> "$FAKE_UV_LOG"
case "$*" in
  *published-tags*) echo "pihole traefik"; exit "${FAKE_LISTING_EXIT:-0}" ;;
  *homelab_render*) exit "${FAKE_RENDER_EXIT:-0}" ;;
esac
"""


@pytest.fixture
def fake_uv(tmp_path, monkeypatch) -> Path:
    """`uv` on PATH as a script that logs its argv, and exits as the test's variables say."""
    bin_dir = fake_bin(tmp_path / "bin", uv=FAKE_UV)
    log = tmp_path / "uv.log"
    monkeypatch.setenv("PATH", path_with(bin_dir))
    monkeypatch.setenv("FAKE_UV_LOG", str(log))
    return log


def render_args(tmp_path: Path) -> list[str]:
    return ["render", "--tree", str(tmp_path), "--out", "out", "--revision", "a" * 40]


def test_main_renders_the_listed_tags_with_the_pinned_commit(tmp_path, fake_uv):
    assert job.main(render_args(tmp_path)) == 0
    rendered = fake_uv.read_text().splitlines()[-1]
    assert f"--revision {'a' * 40}" in rendered
    assert rendered.endswith("--role pihole --role traefik")


@pytest.mark.parametrize("variable", ["FAKE_LISTING_EXIT", "FAKE_RENDER_EXIT"])
def test_main_fails_when_a_render_step_fails(tmp_path, monkeypatch, fake_uv, variable):
    monkeypatch.setenv(variable, "3")
    assert job.main(render_args(tmp_path)) == 1
