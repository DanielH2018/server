"""`checksum/restart`, the outside-role reference test and the self-rollouts census (D3.3c).

The value's pairs run on fixture directories through `restart_values`. cloudflare-ddns and
bento-pdf render for real through `homelab_render.cluster`, with a fixture `.source` in
`tmp_path`. The outside-role test reads a placeholder render of every tag, so this module
carries a shard weight.

Run: uv run pytest scripts/renderer/tests/test_renderer_restart.py
"""

import pytest

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from lib.repo_paths import HOST_VARS, K8S_ROLES
from renderer import homelab_render
from renderer.renderer_lib.cluster import (
    RenderedTag,
    RenderError,
    flux_owned_tags,
    select_tags,
)
from renderer.renderer_lib.placeholders import PlaceholderRender, sentinel
from renderer.renderer_lib.restart import (
    RESTART_ANNOTATION,
    any_pod_template,
    pod_references,
    restart_targets,
    restart_values,
    stamp,
)
from renderer.renderer_lib.task_inputs import include_vars
from renderer.shadow_diff import stub_file_text
from renderer.standalone_map import fleet_render

KEYS = ("alpha_token", "beta_token")
SOURCE = {"alpha_token": "a" * 64, "beta_token": "b" * 64}
SECRET = (
    "apiVersion: v1\nkind: Secret\nmetadata:\n  name: {name}\n"
    'stringData:\n  token: "${{{key}}}"\n'
)
DEPLOYMENT = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {name}\nspec:\n"
    "  template:\n    spec:\n      containers:\n        - name: app\n          env:\n"
    "            - name: TOKEN\n              valueFrom:\n                secretKeyRef:\n"
    "                  name: {secret}\n                  key: token\n"
)


def _two_workloads(comment: str = "") -> dict[str, dict[str, str]]:
    """Two Deployments, each reading its own Secret, and a third reading nothing."""
    files = {
        "secret-a.yaml": comment + SECRET.format(name="a", key="alpha_token"),
        "secret-b.yaml": SECRET.format(name="b", key="beta_token"),
        "deploy-a.yaml": DEPLOYMENT.format(name="one", secret="a"),
        "deploy-b.yaml": DEPLOYMENT.format(name="two", secret="b"),
        "plain.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: three\n"
        "spec:\n  template:\n    spec:\n      containers:\n        - name: app\n",
    }
    return {"fixture": files}


def _values(source=SOURCE, comment="", targets=("one", "two", "three")):
    return restart_values("fixture", _two_workloads(comment), targets, KEYS, source)


def test_the_checksum_moves_when_a_source_digest_moves():
    moved = _values({**SOURCE, "alpha_token": "c" * 64})
    assert moved["one"] != _values()["one"]


def test_the_checksum_stays_put_for_a_comment_edit():
    assert _values(comment="# a reworded comment\n") == _values()


def test_a_change_to_one_secret_moves_only_the_workloads_that_name_it():
    before, after = _values(), _values({**SOURCE, "beta_token": "c" * 64})
    assert after["one"] == before["one"]
    assert after["two"] != before["two"]


def test_a_target_that_names_no_config_map_or_secret_gets_no_value():
    assert "three" not in _values()


def test_a_key_missing_from_source_is_flagged():
    with pytest.raises(RenderError, match="alpha_token has no line in cluster-vars"):
        _values({"beta_token": "b" * 64})


def test_a_target_that_names_no_rendered_workload_is_flagged():
    with pytest.raises(RenderError, match="restart target four"):
        _values(targets=("four",))


INCLUDE = """- name: Deploy fixture
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_rollout: ""
    manifests_extra_rollouts: >-
      {{ [{'name': 'fixture-redis', 'restart_on': []}] if redis | bool else [] }}
"""


@pytest.mark.parametrize("redis, targets", [(True, ["fixture-redis"]), (False, [])])
def test_an_extra_rollouts_list_built_by_an_expression_resolves(
    tmp_path, redis, targets
):
    """authelia builds its list inline, which renders as text before it is a list."""
    (tmp_path / "fixture" / "tasks").mkdir(parents=True)
    (tmp_path / "fixture" / "tasks" / "main.yml").write_text(INCLUDE)
    assert restart_targets("fixture", {"redis": redis}, roles=tmp_path) == targets


def test_a_secret_shipped_as_a_sops_manifest_hashes_its_keys_source_digest():
    files = {"deploy.yaml": DEPLOYMENT.format(name="one", secret="sealed")}
    # The entry is the sentinel render `PlaceholderRender.sops_manifests` holds.
    sops = {
        "sealed.yaml": SECRET.format(name="sealed", key="x").replace(
            "${x}", sentinel("alpha_token")
        )
    }

    def value(source):
        return restart_values("fixture", {"f": files}, ["one"], KEYS, source, sops)

    assert value(SOURCE) != value({**SOURCE, "alpha_token": "c" * 64})


def test_stamp_inserts_one_line_and_keeps_every_other_byte():
    text = DEPLOYMENT.format(name="one", secret="a").replace(
        "  template:\n", "  template:\n    metadata:\n      labels:\n        app: one\n"
    )
    stamped = stamp(text, {"one": "f" * 64}, "fixture")
    assert (
        stamped.replace(
            f'      annotations:\n        {RESTART_ANNOTATION}: "{"f" * 64}"\n', ""
        )
        == text
    )
    template = yaml_fast.safe_load(stamped)["spec"]["template"]
    assert template["metadata"]["annotations"] == {RESTART_ANNOTATION: "f" * 64}


def test_stamp_of_a_flow_style_template_is_flagged():
    text = "kind: Deployment\nmetadata:\n  name: one\nspec:\n  template: {spec: {}}\n"
    with pytest.raises(RenderError, match="not block style"):
        stamp(text, {"one": "f" * 64}, "fixture")


CLOUDFLARE_KEYS = (
    "cloudflare_dns_token",
    "cloudflare_ddns_direct_push_token",
    "cloudflare_ddns_proxied_push_token",
)


@pytest.fixture
def source_file(tmp_path):
    """A fixture `.source` recording cloudflare-ddns's three keys."""
    path = tmp_path / "cluster-vars.source"
    path.write_text("".join(f"{key} {'d' * 64}\n" for key in CLOUDFLARE_KEYS))
    return path


@pytest.fixture
def cluster_file(tmp_path):
    path = tmp_path / "cluster-vars.sops.env"
    path.write_text("k=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n")
    return path


def _annotations(out, tag: str) -> dict[str, str | None]:
    """Each rendered workload of `tag` mapped to its `checksum/restart`, or None."""
    found = {}
    for path in sorted((out / "cluster" / tag).glob("*.yaml")):
        for obj in yaml_fast.safe_load_all(path.read_text()):
            template = any_pod_template(obj)
            if template is not None:
                annotations = (template.get("metadata") or {}).get("annotations") or {}
                found[obj["metadata"]["name"]] = annotations.get(RESTART_ANNOTATION)
    return found


def _render(out, tag, owned, source_file, cluster_file, stubs=None):
    return homelab_render.cluster(
        out,
        [tag],
        stubs,
        flux_owned=lambda: owned,
        cluster_file=cluster_file,
        source_file=source_file,
    )


def test_cloudflare_ddns_two_deployments_each_get_the_annotation(
    tmp_path, source_file, cluster_file
):
    out = tmp_path / "out"
    owned = {"cloudflare-ddns"}
    assert (
        _render(out, "cloudflare-ddns", owned, source_file, cluster_file) == RENDER_OK
    )
    values = _annotations(out, "cloudflare-ddns")
    assert set(values) == {"cloudflare-ddns-direct", "cloudflare-ddns-proxied"}
    assert all(values.values())


def test_the_stub_and_placeholder_renders_of_a_flux_owned_tag_carry_the_same_value(
    tmp_path, source_file, cluster_file
):
    stubs = tmp_path / "stubs.yml"
    stubs.write_text(stub_file_text())
    owned = {"cloudflare-ddns"}
    sides = {}
    for name, mode in (("placeholder", None), ("stub", stubs)):
        out = tmp_path / name
        code = _render(out, "cloudflare-ddns", owned, source_file, cluster_file, mode)
        assert code == RENDER_OK
        sides[name] = _annotations(out, "cloudflare-ddns")
    assert sides["stub"] == sides["placeholder"]
    assert all(sides["stub"].values())


def test_bento_pdf_gets_no_annotation_and_renders_as_if_unowned(
    tmp_path, source_file, cluster_file
):
    texts = {}
    for name, owned in (("owned", {"bento-pdf"}), ("unowned", set())):
        out = tmp_path / name
        assert _render(out, "bento-pdf", owned, source_file, cluster_file) == RENDER_OK
        texts[name] = {
            p.name: p.read_text() for p in (out / "cluster" / "bento-pdf").iterdir()
        }
    assert texts["owned"] == texts["unowned"]
    assert _annotations(tmp_path / "owned", "bento-pdf") == {"bento-pdf": None}


# A reference a flux-owned pod may make to an object its own role does not render, as
# (tag, kind, name), each with its reason. Empty: no flux-owned role makes one.
OUTSIDE_ROLE_EXEMPT: frozenset[tuple[str, str, str]] = frozenset()


def outside_references(render, tags) -> list[tuple[str, str, str]]:
    """Each `(tag, kind, name)` a pod of `tags` names and its own render does not hold.

    A Secret the tag ships as a SOPS manifest counts as its own.
    """
    found = []
    forms = {t.tag: t for t in render.tags}
    for tag in sorted(tags):
        docs = [
            obj
            for files in forms[tag].directories().values()
            for text in files.values()
            for obj in yaml_fast.safe_load_all(text)
            if isinstance(obj, dict)
        ]
        docs += [
            obj
            for text in (render.sops_manifests.get(tag) or {}).values()
            for obj in yaml_fast.safe_load_all(text or "")
            if isinstance(obj, dict)
        ]
        own = {(d.get("kind"), (d.get("metadata") or {}).get("name")) for d in docs}
        for obj in docs:
            template = any_pod_template(obj)
            for kind, name in sorted(pod_references(template or {})):
                if (kind, name) not in own and (
                    tag,
                    kind,
                    name,
                ) not in OUTSIDE_ROLE_EXEMPT:
                    found.append((tag, kind, name))
    return found


@pytest.fixture(scope="module")
def fleet():
    return fleet_render()


def test_no_flux_owned_pod_names_another_roles_secret_or_config_map_is_clean(fleet):
    assert outside_references(fleet, flux_owned_tags()) == []


def test_the_outside_role_test_resolves_peanuts_three_references(fleet):
    """peanut is the named member: two `secretKeyRef`s and a `secretName` volume."""
    (deployment,) = [
        obj
        for t in fleet.tags
        if t.tag == "peanut"
        for text in t.files.values()
        for obj in yaml_fast.safe_load_all(text)
        if isinstance(obj, dict) and obj.get("kind") == "Deployment"
    ]
    template = any_pod_template(deployment)
    assert template is not None
    assert pod_references(template) == {("Secret", "peanut-secrets")}
    named = [
        env["valueFrom"]["secretKeyRef"]["name"]
        for c in template["spec"]["containers"]
        for env in c.get("env") or []
        if "secretKeyRef" in (env.get("valueFrom") or {})
    ] + [
        v["secret"]["secretName"] for v in template["spec"]["volumes"] if "secret" in v
    ]
    assert named == ["peanut-secrets"] * 3
    assert outside_references(fleet, {"peanut"}) == []


def test_a_flux_owned_role_whose_pod_names_another_roles_secret_is_flagged():
    files: dict[str, str] = {
        "deploy.yaml": DEPLOYMENT.format(name="one", secret="peanut-secrets")
    }
    render = PlaceholderRender([RenderedTag("fixture", files)], {}, {})
    assert outside_references(render, {"fixture"}) == [
        ("fixture", "Secret", "peanut-secrets")
    ]


def self_rolled(tags) -> set[str]:
    """The tags whose role passes a non-empty `manifests_self_rollouts`.

    A Jinja string, as observability passes, counts as non-empty.
    """
    return {
        tag
        for tag in tags
        if (K8S_ROLES / tag / "tasks").is_dir()
        and include_vars(K8S_ROLES / tag).get("manifests_self_rollouts")
        not in (None, [], "")
    }


def test_no_flux_owned_role_rolls_its_own_workloads_is_clean():
    """Until D5.1e, nothing restarts a self-rolled workload under Flux (D3 *Restart triggers*)."""
    tags = select_tags(None)
    assert {"pihole", "observability"} <= self_rolled(tags)
    assert self_rolled(flux_owned_tags()) == set()


def test_a_self_rolled_role_marked_owner_flux_is_flagged(tmp_path):
    text = (HOST_VARS / "daniel-box.yml").read_text()
    marked = text.replace(
        "  - name: pihole\n", "  - name: pihole\n    owner: flux\n", 1
    )
    assert marked != text
    (tmp_path / "daniel-box.yml").write_text(marked)
    assert self_rolled(flux_owned_tags(host_vars=tmp_path)) == {"pihole"}
