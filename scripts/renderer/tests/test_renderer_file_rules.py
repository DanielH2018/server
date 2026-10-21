"""`render_tag` applies the manifests role's file rules.

Those are the shared defaults, the labelling wrapper, the claim files and the deferred
directory. One role stands for each rule: littlelink is prune-armed and takes both shared
defaults, authelia opts out of the prune, headlamp defers its observability RBAC, and scrutiny
declares two claims. Each render reads the real tree and a fresh stub file.

Run: uv run pytest scripts/renderer/tests/test_renderer_file_rules.py
"""

import re

import pytest

from deploy_tools.render_targets import render_targets
from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from lib.render_guard import SHARED_TPL
from renderer.renderer_lib.task_inputs import (
    BUILT_IMAGE_TAGS,
    SET_FACTS,
    TASK_COMPUTED,
    _tasks,
    include_vars,
)

ROLE_LABEL = "homelab/role"


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def _docs(text: str) -> list[dict]:
    return [doc for doc in yaml_fast.safe_load_all(text) if doc]


def _labels(text: str) -> list[str | None]:
    return [
        (doc.get("metadata") or {}).get("labels", {}).get(ROLE_LABEL)
        for doc in _docs(text)
    ]


def test_an_armed_role_takes_the_shared_defaults_and_labels_every_document(stubs):
    rendered = cluster.render_tag("littlelink", stubs)
    assert sorted(rendered.files) == [
        "deployment.yaml",
        "ingressroute.yaml",
        "kustomization.yaml",
        "service.yaml",
    ]
    for name in ("deployment.yaml", "ingressroute.yaml", "service.yaml"):
        labels = _labels(rendered.files[name])
        assert labels and set(labels) == {"littlelink"}, name
    kinds = {doc["kind"] for doc in _docs(rendered.files["ingressroute.yaml"])}
    assert kinds == {"IngressRoute"}
    assert rendered.deferred == {}


def test_an_unarmed_role_renders_without_the_label(stubs):
    assert include_vars(cluster.K8S_ROLES / "authelia")["manifests_prune"] is False
    rendered = cluster.render_tag("authelia", stubs)
    manifests = {n: t for n, t in rendered.files.items() if n != cluster.KUSTOMIZATION}
    assert manifests
    for name, text in manifests.items():
        labels = _labels(text)
        assert labels and set(labels) == {None}, name


def test_a_deferred_file_renders_unlabelled_into_its_own_directory(stubs):
    deferred = include_vars(cluster.K8S_ROLES / "headlamp")["manifests_deferred_files"]
    rendered = cluster.render_tag("headlamp", stubs)
    assert sorted(rendered.deferred) == sorted([*deferred, cluster.KUSTOMIZATION])
    assert not set(deferred) & set(rendered.files)
    for name in deferred:
        labels = _labels(rendered.deferred[name])
        assert labels and set(labels) == {None}, name
    kustomization = yaml_fast.safe_load(rendered.deferred[cluster.KUSTOMIZATION])
    assert kustomization["resources"] == sorted(deferred)
    assert sorted(rendered.directories()) == [
        "headlamp",
        "headlamp-deferred",
        "headlamp-verify",
    ]


def test_a_role_with_two_claims_writes_two_labelled_claim_files(stubs):
    rendered = cluster.render_tag("scrutiny", stubs)
    claims = sorted(n for n in rendered.files if n.startswith("claim-"))
    assert len(claims) == 2
    for name in claims:
        (doc,) = _docs(rendered.files[name])
        assert doc["kind"] == "PersistentVolumeClaim"
        assert f"claim-{doc['metadata']['name']}.yaml" == name
        assert doc["metadata"]["labels"][ROLE_LABEL] == "scrutiny"


def test_a_listed_file_with_no_template_is_flagged(tmp_path):
    with pytest.raises(cluster.RenderError, match="has no template"):
        cluster._sources("littlelink", {"no-such-file.yaml"}, False)


def test_a_role_including_the_manifests_role_twice_is_flagged(tmp_path):
    tasks = tmp_path / "role" / "tasks"
    tasks.mkdir(parents=True)
    include = "- ansible.builtin.include_role:\n    name: k8s/manifests\n"
    (tasks / "main.yml").write_text(include + include)
    with pytest.raises(ValueError, match="2 times"):
        include_vars(tmp_path / "role")


def test_an_include_of_another_manifests_entry_point_is_not_the_render(tmp_path):
    tasks = tmp_path / "role" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        "- ansible.builtin.include_role:\n    name: k8s/manifests\n"
        "  vars:\n    manifests_prune: false\n"
        "- ansible.builtin.include_role:\n    name: k8s/manifests\n"
        "    tasks_from: drain.yml\n"
        "  vars:\n    manifests_rollout: ''\n"
    )
    assert include_vars(tmp_path / "role") == {"manifests_prune": False}


def _task_computed_readers() -> set[str]:
    """The render targets whose templates or defaults name a `TASK_COMPUTED` value."""
    word = re.compile(rf"(?<!\w)({'|'.join(sorted(TASK_COMPUTED))})(?!\w)")
    readers = set()
    for tag in render_targets(K8S_HOST):
        role = cluster.K8S_ROLES / tag
        files = [*role.glob("templates/**/*"), *role.glob("defaults/*.yml")]
        if any(p.is_file() and word.search(p.read_text()) for p in files):
            readers.add(tag)
    return readers


def _set_fact_names() -> set[str]:
    """Every name a `set_fact` or a `register` in a k8s role's tasks sets."""
    names = set()
    for path in cluster.K8S_ROLES.glob("*/tasks/*.yml"):
        for task in _tasks(yaml_fast.safe_load(path.read_text())):
            names |= set(task.get("ansible.builtin.set_fact") or {})
            if isinstance(task.get("register"), str):
                names.add(task["register"])
    return {n for n in names if not n.startswith("manifests_")}


def test_task_computed_names_every_fact_a_template_or_default_reads():
    """`==`, so a fact a template starts reading fails here rather than in a render.

    No template reads a live fact, so every name here is one the renderer computes. A line
    that opens with `#` is a comment, in YAML and in a Dockerfile alike, and reads nothing.
    """
    files = [*SHARED_TPL.glob("**/*.j2")]
    for tag in render_targets(K8S_HOST):
        role = cluster.K8S_ROLES / tag
        files += [*role.glob("templates/**/*"), *role.glob("defaults/*.yml")]
    text = "\n".join(
        line
        for p in files
        if p.is_file()
        for line in p.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    read = {
        name
        for name in _set_fact_names()
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text)
    }
    assert "kuma_declarations" in read
    assert read == TASK_COMPUTED


def test_every_reader_of_a_set_fact_value_derives_it():
    """Each tag that reads a `TASK_COMPUTED` value is a role whose `SET_FACTS` computes it.

    `k8s_built_image_tags` is the exception: `derived_facts` sets it for every role.
    """
    readers = _task_computed_readers()
    # Non-vacuity: uptime-kuma reads its declarations, nut its content tag.
    assert {"uptime-kuma", "nut"} <= readers
    for tag in readers:
        role = cluster.K8S_ROLES / tag
        files = [*role.glob("templates/**/*"), *role.glob("defaults/*.yml")]
        text = "\n".join(p.read_text() for p in files if p.is_file())
        derived = {fact for _, fact in SET_FACTS.get(tag, ())} | {BUILT_IMAGE_TAGS}
        for name in TASK_COMPUTED:
            if re.search(rf"(?<!\w){name}(?!\w)", text):
                assert name in derived, (tag, name)


@pytest.mark.parametrize("tag", cluster.select_tags(None))
def test_every_default_tag_renders_under_the_strict_undefined(tag, stubs):
    """The verify-by of R1's PR 3: every tag `cluster` renders by default renders clean."""
    rendered = cluster.render_tag(tag, stubs)
    # A tag whose only content is vendored, traefik-crds, renders no template of its own;
    # attach_vendored adds its files later.
    assert set(rendered.files) - {cluster.KUSTOMIZATION} or cluster.vendored_files(
        tag
    ), tag


def test_the_default_tag_set_is_every_render_target():
    """Non-vacuity for the parametrisation above, the tags reading task-computed values too."""
    tags = set(cluster.select_tags(None))
    assert len(tags) >= 56 and {"traefik", "nut", "uptime-kuma", "janitorr"} <= tags
    assert tags == set(render_targets(K8S_HOST))
