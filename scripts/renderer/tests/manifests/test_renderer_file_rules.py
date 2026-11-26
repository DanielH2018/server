"""`render_tag` applies the manifests role's file rules.

Those are the shared defaults, the labelling wrapper, the claim files and the deferred
directory. One role stands for each rule: littlelink is prune-armed and takes both shared
defaults, authelia opts out of the prune, headlamp defers its observability RBAC, and scrutiny
declares two claims. Each render reads the real tree and a fresh stub file.

Run: uv run pytest scripts/renderer/tests/manifests/test_renderer_file_rules.py
"""

import pytest

from renderer.renderer_lib.targets import render_targets
from lib import yaml_fast
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs
from renderer.renderer_lib.task_inputs import render_defaults

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
    assert render_defaults(cluster.K8S_ROLES / "authelia")["manifests_prune"] is False
    rendered = cluster.render_tag("authelia", stubs)
    manifests = {n: t for n, t in rendered.files.items() if n != cluster.KUSTOMIZATION}
    assert manifests
    for name, text in manifests.items():
        labels = _labels(text)
        assert labels and set(labels) == {None}, name


def test_a_deferred_file_renders_unlabelled_into_its_own_directory(stubs):
    deferred = render_defaults(cluster.K8S_ROLES / "headlamp")[
        "manifests_deferred_files"
    ]
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


def test_a_declared_secret_renders_each_field_from_its_sops_key(stubs):
    rendered = cluster.render_tag("karakeep", stubs)
    (doc,) = _docs(rendered.files["secret-karakeep.yaml"])
    assert doc["kind"] == "Secret"
    assert doc["metadata"]["labels"][ROLE_LABEL] == "karakeep"
    assert doc["stringData"]["NEXTAUTH_SECRET"] == stubs["karakeep_nextauth_secret"]
    assert not any(n.startswith("secret.") for n in rendered.files)


def test_a_declared_secret_that_reuses_a_rendered_file_name_is_flagged(stubs):
    entry = {"name": "soularr", "string_data": {"key": "slskd_wireguard_private_key"}}
    with pytest.raises(cluster.RenderError, match="already render"):
        cluster.render_tag("slskd", {**stubs, "render_secrets": [entry]})


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ({"name": "a", "string_data": {"F": "not_a_sops_key"}}, "not SOPS keys"),
        ({"name": "a", "string_data": {"F": "k"}, "type": "tls"}, "unknown keys"),
        ({"name": "a", "string_data": {}}, "non-empty `string_data`"),
        ({"string_data": {"F": "k"}}, "needs a `name`"),
    ],
)
def test_a_declared_secret_of_the_wrong_shape_is_flagged(entry, reason):
    with pytest.raises(cluster.RenderError, match=reason):
        cluster.secret_entries("widget", [entry], {"k": "v"})


def test_a_declared_secret_of_the_right_shape_is_clean():
    entry = {"name": "a", "namespace": "ns", "string_data": {"F": "k"}}
    assert cluster.secret_entries("widget", [entry], {"k": "v"}) == [entry]


def test_a_render_key_that_does_not_resolve_is_flagged(stubs):
    unresolvable = {**stubs, "traefik_k8s_manage_crowdsec": "{{ no_such_name }}"}
    with pytest.raises(cluster.RenderError, match=r"render keys \['manifests_files'"):
        cluster.render_tag("traefik", unresolvable)


def test_a_listed_file_with_no_template_is_flagged(tmp_path):
    with pytest.raises(cluster.RenderError, match="has no template"):
        cluster._sources("littlelink", {"no-such-file.yaml"}, False)


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
    assert len(tags) >= 56 and {"traefik", "nut", "janitorr"} <= tags
    assert tags == set(render_targets())
