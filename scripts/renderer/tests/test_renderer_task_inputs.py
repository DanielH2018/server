"""`task_inputs.derived_facts` computes what the role tasks compute, by their own expressions.

Each derived value is held equal to ansible-core's `Templar` evaluating the same task
expression with the same variables, through the helper
`ansible/tests/deploy/test_image_builder_content_tag.py` uses. Every check renders against the
real tree and a fresh stub file.

Run: uv run pytest scripts/renderer/tests/test_renderer_task_inputs.py
"""

import re
from pathlib import Path

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
    (role, fact) for role, steps in task_inputs.SET_FACTS.items() for _, fact in steps
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


def _builder_role(tmp_path, defaults: str = "", tasks_vars: str = "") -> Path:
    role = tmp_path / "role"
    (role / "tasks").mkdir(parents=True)
    (role / "defaults").mkdir()
    (role / "defaults" / "main.yml").write_text(defaults or "---\n")
    include = "- ansible.builtin.include_role:\n    name: k8s/image-builder\n"
    (role / "tasks" / "main.yml").write_text(include + tasks_vars)
    return role


def test_builds_in_defaults_are_read_in_order_with_their_when(tmp_path):
    """The include left in tasks names no image, so it adds no site."""
    role = _builder_role(
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


def test_builds_in_defaults_and_vars_on_the_include_are_flagged(tmp_path):
    role = _builder_role(
        tmp_path,
        "image_builder_builds:\n  - image_builder_name: first\n",
        "  vars:\n    image_builder_name: second\n",
    )
    with pytest.raises(ValueError, match="image_builder_builds and also"):
        task_inputs.image_builder_sites(role)


def _manifests_role(tmp_path, defaults: str, tasks_vars: str = "") -> Path:
    role = tmp_path / "role"
    (role / "tasks").mkdir(parents=True)
    (role / "defaults").mkdir()
    (role / "defaults" / "main.yml").write_text(defaults)
    include = "- ansible.builtin.include_role:\n    name: k8s/manifests\n"
    (role / "tasks" / "main.yml").write_text(include + tasks_vars)
    return role


def test_include_vars_reads_only_the_render_keys_from_defaults(tmp_path):
    role = _manifests_role(
        tmp_path,
        "manifests_rollout: demo\nmanifests_rollout_timeout: 900s\nother: 1\n",
        "  vars:\n    manifests_rollout_kind: daemonset\n",
    )
    assert task_inputs.include_vars(role) == {
        "manifests_rollout": "demo",
        "manifests_rollout_kind": "daemonset",
    }


def test_a_render_key_in_defaults_and_on_the_include_is_flagged(tmp_path):
    role = _manifests_role(
        tmp_path,
        "manifests_rollout: demo\n",
        "  vars:\n    manifests_rollout: other\n",
    )
    with pytest.raises(ValueError, match=r"manifests_rollout.*in both"):
        task_inputs.include_vars(role)


def _render_keys_on_includes(roles: Path) -> dict[str, set[str]]:
    """Each role whose tasks pass a `RENDER_KEYS` key or an `image_builder_*` var, with the keys."""
    found: dict[str, set[str]] = {}
    for path in sorted(roles.glob("*/tasks/*.yml")):
        for task in task_inputs._tasks(yaml_fast.safe_load(path.read_text()) or []):
            keys = {
                key
                for key in task.get("vars") or {}
                if key in task_inputs.RENDER_KEYS or key.startswith("image_builder_")
            }
            if keys and any(
                isinstance(task.get(module), dict)
                and task[module].get("name") in {"k8s/manifests", "k8s/image-builder"}
                for module in task_inputs._INCLUDE_MODULES
            ):
                found.setdefault(path.parts[-3], set()).update(keys)
    return found


def test_no_role_keeps_render_metadata_on_its_include_is_clean():
    assert _render_keys_on_includes(K8S_ROLES) == {}
    pihole = task_inputs.include_vars(K8S_ROLES / "pihole")
    assert pihole["manifests_deferred_files"] == ["deployment-2.yaml"]
    assert pihole["manifests_deferred_dir_name"] == "pihole-instance-2"


def test_render_metadata_on_an_include_is_flagged(tmp_path):
    _manifests_role(tmp_path, "---\n", "  vars:\n    manifests_rollout: demo\n")
    assert _render_keys_on_includes(tmp_path) == {"role": {"manifests_rollout"}}
