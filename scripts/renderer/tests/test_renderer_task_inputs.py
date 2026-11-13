"""`task_inputs` reads a role's defaults, and each content tag equals image-builder's expression.

Each content tag is held equal to image-builder's expression evaluated with the same variables, through the helper `tests/deploy/test_image_builder_content_tag.py`
uses. A check on the real tree renders it with a fresh stub file.

Run: uv run pytest scripts/renderer/tests/test_renderer_task_inputs.py
"""

import re
from pathlib import Path

import pytest

from _image_builder_tag import expression_content_tag
from lib.k8s_roles import role_dirs
from lib.repo_paths import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster, derived, task_inputs
from renderer.renderer_lib.context import load_stubs

# `sha-` and 12 hex digits, the shape registry-gc.sh's prune keys on.
TAG_RE = re.compile(r"^sha-[0-9a-f]{12}$")

SITES = [
    (role.name, index)
    for role in role_dirs()
    for index, _ in enumerate(task_inputs.image_builder_sites(role))
]


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _ctx(role: str, stubs: dict) -> dict:
    return cluster.tag_context(K8S_ROLES / role, role, stubs)


def test_the_site_census_finds_all_ten_builds():
    """Non-vacuity for the parametrisation below; n8n builds two images."""
    assert len(SITES) == 10
    assert {("homelab-mcp", 0), ("n8n", 0), ("n8n", 1)} <= set(SITES)


@pytest.mark.parametrize(("role", "index"), SITES)
def test_the_content_tag_equals_the_one_templar_computes(role, index, stubs):
    site = task_inputs.image_builder_sites(K8S_ROLES / role)[index]
    variables = task_inputs.site_variables(site, _ctx(role, stubs))
    tag = derived.content_tag(task_inputs.context_data(variables))
    assert TAG_RE.match(tag), tag
    assert tag == expression_content_tag(variables)


def test_dropping_a_context_file_changes_the_tag(stubs):
    """The red half: a build input the renderer leaves out must move the tag."""
    (site,) = task_inputs.image_builder_sites(K8S_ROLES / "homelab-mcp")
    files = site.vars["image_builder_context_files"]
    assert len(files) >= 2
    dropped = task_inputs.BuildSite(
        {**site.vars, "image_builder_context_files": dict(list(files.items())[1:])},
        site.when,
    )
    ctx = _ctx("homelab-mcp", stubs)
    before = derived.content_tag(
        task_inputs.context_data(task_inputs.site_variables(site, ctx))
    )
    after = derived.content_tag(
        task_inputs.context_data(task_inputs.site_variables(dropped, ctx))
    )
    assert TAG_RE.match(before) and TAG_RE.match(after)
    assert before != after


def test_a_consumer_names_its_derived_tag(stubs):
    """The tag reaches the role default that reads it, through the second context pass."""
    tags = task_inputs.built_image_tags(K8S_ROLES / "nut", _ctx("nut", stubs))
    rendered = cluster.render_tag("nut", stubs)
    assert f"/nut:{tags['nut']}" in rendered.files["deployment.yaml"]


def _role(tmp_path, defaults: str | None = "---\n", tasks: str | None = None) -> Path:
    role = tmp_path / "role"
    role.mkdir()
    if defaults is not None:
        (role / "defaults").mkdir()
        (role / "defaults" / "main.yml").write_text(defaults)
    if tasks is not None:
        (role / "tasks").mkdir()
        (role / "tasks" / "main.yml").write_text(tasks)
    return role


BARE_INCLUDE = "- ansible.builtin.include_role:\n    name: k8s/image-builder\n"


def test_builds_in_defaults_are_read_in_order_with_their_when(tmp_path):
    role = _role(
        tmp_path,
        "image_builder_builds:\n"
        "  - image_builder_name: first\n"
        "  - image_builder_name: second\n"
        "    when: lever | bool\n",
    )
    assert task_inputs.image_builder_sites(role) == [
        task_inputs.BuildSite({"image_builder_name": "first"}),
        task_inputs.BuildSite({"image_builder_name": "second"}, ("lever | bool",)),
    ]


def test_include_vars_reads_only_the_render_keys_from_defaults(tmp_path):
    role = _role(tmp_path, "manifests_rollout: demo\nmanifests_rollout_timeout: 900s\n")
    assert task_inputs.include_vars(role) == {"manifests_rollout": "demo"}


def test_a_role_with_no_defaults_file_raises(tmp_path):
    role = _role(tmp_path, None, BARE_INCLUDE)
    with pytest.raises(ValueError, match=r"has no defaults/main\.yml"):
        task_inputs.include_vars(role)
    with pytest.raises(ValueError, match=r"has no defaults/main\.yml"):
        task_inputs.image_builder_sites(role)
