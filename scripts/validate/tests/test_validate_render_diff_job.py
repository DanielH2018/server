"""The render-diff job's verdict on fixture trees, and its reading of the PR body.

The workflow that calls it runs only on GitHub. Each case here is a verdict the workflow's
red-proofs show on a real render.

Run: uv run pytest scripts/validate/tests/test_validate_render_diff_job.py
"""

import shutil
from pathlib import Path

import pytest

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
