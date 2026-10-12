"""`task_inputs.derived_facts` computes what the role tasks compute, by their own expressions.

Each derived value is held equal to ansible-core's `Templar` evaluating the same task
expression with the same variables, through the helper
`ansible/tests/deploy/test_image_builder_content_tag.py` uses. Every check renders against the
real tree and a fresh stub file.

Run: uv run pytest scripts/renderer/tests/test_renderer_task_inputs.py
"""

import json
import re

import pytest

from _image_builder_tag import templar_content_tag, templar_value
from lib import yaml_fast
from lib.repo_paths import K8S_ROLES
from renderer import shadow_diff
from renderer.renderer_lib import cluster, task_inputs
from renderer.renderer_lib.context import load_stubs

# `sha-` and 12 hex digits, the shape registry-gc.sh's prune keys on.
TAG_RE = re.compile(r"^sha-[0-9a-f]{12}$")

SITES = [
    (role.name, index)
    for role in sorted(K8S_ROLES.iterdir())
    for index, _ in enumerate(task_inputs.image_builder_sites(role))
]

CHECKED_FACTS = [
    (role, fact)
    for role, steps in task_inputs.SET_FACTS.items()
    for _, fact in steps
    # Its expression renders uptime-kuma's monitors through the repo's own filters, which a
    # bare Templar does not load. test_uptime_kuma_indexes_every_declaration covers it.
    if fact != "kuma_declarations"
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
    tag = task_inputs.content_tag(variables)
    assert TAG_RE.match(tag), tag
    assert tag == templar_content_tag(variables)


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
    before = task_inputs.content_tag(task_inputs.site_variables(site, ctx))
    after = task_inputs.content_tag(task_inputs.site_variables(dropped, ctx))
    assert TAG_RE.match(before) and TAG_RE.match(after)
    assert before != after


def test_a_consumer_names_its_derived_tag(stubs):
    """The tag reaches the role default that reads it, through the second context pass."""
    tags = task_inputs.built_image_tags(K8S_ROLES / "nut", _ctx("nut", stubs))
    rendered = cluster.render_tag("nut", stubs)
    assert f"/nut:{tags['nut']}" in rendered.files["deployment.yaml"]


@pytest.mark.parametrize(("role", "fact"), CHECKED_FACTS)
def test_a_set_fact_equals_the_value_templar_computes(role, fact, stubs):
    role_dir = K8S_ROLES / role
    ctx = _ctx(role, stubs)
    facts = task_inputs.derived_facts(role_dir, ctx)
    tasks_file = next(f for f, name in task_inputs.SET_FACTS[role] if name == fact)
    expression, _ = task_inputs.set_fact_task(role_dir / "tasks" / tasks_file, fact)
    variables = {
        **ctx,
        **{k: v for k, v in facts.items() if k != fact},
        "playbook_dir": str(K8S_ROLES.parents[1]),
        "role_path": str(role_dir),
    }
    assert facts[fact] == templar_value(expression, variables, role_dir)


def test_uptime_kuma_indexes_every_declaration(stubs):
    """`kuma_declarations` is read through `default({})`, so an empty one would still render.

    The role's own task asserts more than 50 declarations, and so does this.
    """
    rendered = cluster.render_tag("uptime-kuma", stubs)
    (configmap,) = [
        doc
        for doc in yaml_fast.safe_load_all(
            rendered.files["status-page-sync-configmap.yaml"]
        )
        if doc
    ]
    assert len(json.loads(configmap["data"]["index.json"])) > 50
