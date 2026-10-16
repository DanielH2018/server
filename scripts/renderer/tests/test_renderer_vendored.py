"""H1.1a's renderer support: vendored files, kustomize patches, `prune: false` and a role's timeout.

Every fixture role lives in `tmp_path`, and `attach_vendored` and `bundle_files` take its tree
as `roles`, so no role in the repo needs to vendor anything. The plan is
docs/target-architecture/host/h1-cluster-config-to-flux.md, *Vendored upstream manifests* and
*Plumbing tags are never pruned*.

Run: uv run pytest scripts/renderer/tests/test_renderer_vendored.py
"""

import pytest

from deploy_tools.bundle_census import census
from deploy_tools.bundle_gates import source
from lib import yaml_fast
from lib.exit_codes import RENDER_FAILED, RENDER_OK
from lib.k8s_roles import role_fields
from renderer import homelab_render, shadow_diff
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE, bundle_files
from renderer.renderer_lib.roundtrip import render_sides, stub_passes
from renderer.renderer_lib.cluster import (
    KUSTOMIZATION,
    RenderedTag,
    attach_vendored,
    kustomization_text,
    write_cluster,
)
from renderer.renderer_lib.bundle import write_bundle
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.placeholders import apply_placeholders, sentinel

TAG = "metallb"
TIMEOUT = "600s"
DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
# Upstream bytes as a release ships them: CRLF line ends and a `${` Flux must never read.
UPSTREAM = (
    "apiVersion: apps/v1\r\nkind: DaemonSet\r\nmetadata:\r\n  name: speaker\r\n"
    'spec:\r\n  args: ["${NODE}"]\r\n'
)
PATCH = "apiVersion: apps/v1\nkind: DaemonSet\nmetadata:\n  name: speaker\nspec: {}\n"
CONFIG = "kind: ConfigMap\nmetadata:\n  name: config\ndata:\n  value: {value}\n"


def _role(roles, defaults: str = "", vendors: bool = True):
    """A fixture role `TAG` under `roles`, vendoring one upstream file and one patch."""
    role = roles / TAG
    (role / "defaults").mkdir(parents=True)
    (role / "defaults" / "main.yml").write_text(defaults or "---\n")
    if vendors:
        (role / "files" / "upstream").mkdir(parents=True)
        (role / "files" / "upstream" / "metallb-native.yaml").write_bytes(
            UPSTREAM.encode()
        )
        (role / "files" / "patches").mkdir()
        (role / "files" / "patches" / "speaker.yaml").write_text(PATCH)
    return roles


def _rendered(value: str = "plain") -> RenderedTag:
    files = {"configmap.yaml": CONFIG.format(value=value)}
    return RenderedTag(TAG, {**files, KUSTOMIZATION: kustomization_text(files)})


def _spec(roles, defaults: str = "", vendors: bool = True) -> dict:
    """The spec of the Kustomization `TAG` that a placeholder render publishes."""
    _role(roles, defaults, vendors)
    result = apply_placeholders([_rendered()], ("email",))
    tags = attach_vendored(result.tags, result.key_sets, roles)
    directories = {n: f for t in tags for n, f in t.directories().items()}
    files = bundle_files(
        directories, result.key_sets, [TAG], TIMEOUT, DOTENV, roles=roles
    )
    return yaml_fast.safe_load(files[f"flux/{TAG}.yaml"])["spec"]


def test_a_vendored_file_and_a_patch_render_into_the_kustomization(tmp_path):
    roles = _role(tmp_path / "roles")
    result = apply_placeholders([_rendered()], ("email",))
    [tag] = attach_vendored(result.tags, result.key_sets, roles)
    assert yaml_fast.safe_load(tag.files[KUSTOMIZATION]) == {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": ["configmap.yaml", "upstream/metallb-native.yaml"],
        "patches": [{"path": "patches/speaker.yaml"}],
    }
    written = write_cluster([tag], tmp_path / "out")
    upstream = tmp_path / "out" / "cluster" / TAG / "upstream" / "metallb-native.yaml"
    assert upstream in written
    assert upstream.read_bytes() == UPSTREAM.encode(), (
        "the vendored bytes reach the cluster unchanged, CRLF and the unescaped ${ included"
    )


def test_a_bundle_with_vendored_files_passes_the_publish_census_and_source_gate(
    tmp_path,
):
    """The publish job's checks walk `cluster/` recursively and read nested files too.

    A patch sits outside `resources`, and the upstream `${NODE}` names no secret key.
    """
    roles = _role(tmp_path / "roles", "flux_prune: false\n")
    result = apply_placeholders([_rendered()], ("email",))
    tags = attach_vendored(result.tags, result.key_sets, roles)
    directories = {n: f for t in tags for n, f in t.directories().items()}
    out = tmp_path / "out"
    write_cluster(tags, out)
    write_bundle(
        bundle_files(directories, result.key_sets, [TAG], TIMEOUT, DOTENV, roles=roles),
        out,
    )
    assert census(out, [TAG]) == []
    assert source(out, [TAG], frozenset({"email"})) == []


def test_a_directory_without_patches_renders_no_patches_key():
    """The kept half: every render before H1.1a stays byte-identical."""
    text = kustomization_text(["service.yaml", "deployment.yaml"])
    assert "patches" not in yaml_fast.safe_load(text)


def test_a_placeholder_in_a_directory_with_vendored_files_is_refused(tmp_path):
    roles = _role(tmp_path / "roles")
    result = apply_placeholders([_rendered(sentinel("email"))], ("email",))
    with pytest.raises(RenderError, match=r"vendors .* placeholders \['email'\]"):
        attach_vendored(result.tags, result.key_sets, roles)


def test_a_placeholder_in_a_tag_that_vendors_nothing_is_attached_unchanged(tmp_path):
    roles = _role(tmp_path / "roles", vendors=False)
    result = apply_placeholders([_rendered(sentinel("email"))], ("email",))
    assert attach_vendored(result.tags, result.key_sets, roles) == result.tags


def test_flux_prune_false_renders_prune_false(tmp_path):
    assert _spec(tmp_path, "flux_prune: false\n")["prune"] is False


def test_a_deferred_directory_takes_its_roles_fields(tmp_path):
    """A plumbing role's deferred objects are its own manifests, so they are never pruned."""
    _role(tmp_path, "flux_prune: false\nmanifests_rollout_timeout: 1200s\n", False)
    tag = RenderedTag(TAG, _rendered().files, _rendered().files)
    result = apply_placeholders([tag], ("email",))
    directories = {n: f for t in result.tags for n, f in t.directories().items()}
    files = bundle_files(
        directories, result.key_sets, [TAG], TIMEOUT, DOTENV, roles=tmp_path
    )
    docs = list(yaml_fast.safe_load_all(files[f"flux/{TAG}.yaml"]))
    assert [
        (d["metadata"]["name"], d["spec"]["prune"], d["spec"]["timeout"]) for d in docs
    ] == [
        (TAG, False, "1200s"),
        (f"{TAG}-deferred", False, "1200s"),
    ]


def test_a_role_that_sets_neither_field_keeps_prune_and_the_default_timeout(tmp_path):
    spec = _spec(tmp_path, vendors=False)
    assert (spec["prune"], spec["timeout"]) == (True, TIMEOUT)


def test_a_roles_rollout_timeout_renders_as_its_kustomization_timeout(tmp_path):
    assert _spec(tmp_path, "manifests_rollout_timeout: 1200s\n")["timeout"] == "1200s"


@pytest.mark.parametrize(
    "defaults",
    [
        "flux_prune: 'false'\n",
        'manifests_rollout_timeout: "{{ metallb_timeout }}"\n',
        "manifests_rollout_timeout: 600\n",
    ],
)
def test_a_field_that_is_not_a_literal_is_a_problem(tmp_path, defaults):
    _role(tmp_path, defaults, vendors=False)
    found, problems = role_fields([TAG], tmp_path)
    assert found == {}
    assert len(problems) == 1 and problems[0].startswith(f"{TAG}'s ")


# The CLI renders real tags from the repo's roles and reads the vendored files from the
# fixture tree it is handed: littlelink holds no placeholder, and speedtest holds one.
@pytest.fixture
def vendoring(tmp_path):
    """A role tree in which littlelink and speedtest each vendor `UPSTREAM` and `PATCH`."""
    roles = tmp_path / "vendored"
    for tag in ("littlelink", "speedtest"):
        (roles / tag / "files" / "upstream").mkdir(parents=True)
        (roles / tag / "files" / "upstream" / "fixture.yaml").write_bytes(
            UPSTREAM.encode()
        )
        (roles / tag / "files" / "patches").mkdir()
        (roles / tag / "files" / "patches" / "speaker.yaml").write_text(PATCH)
    return roles


def _vendored_into(directory) -> bool:
    kustomization = yaml_fast.safe_load((directory / KUSTOMIZATION).read_text())
    upstream = directory / "upstream" / "fixture.yaml"
    return upstream.read_bytes() == UPSTREAM.encode() and {
        "path": "patches/speaker.yaml"
    } in kustomization.get("patches", [])


def test_a_placeholder_render_writes_the_vendored_files(tmp_path, vendoring):
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text(DOTENV)
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out,
        ["littlelink"],
        None,
        flux_owned=lambda: {"littlelink"},
        cluster_file=cluster_file,
        vendored=vendoring,
    )
    assert code == RENDER_OK
    assert _vendored_into(out / "cluster" / "littlelink")


def test_a_placeholder_render_of_a_vendoring_tag_with_a_placeholder_fails(
    tmp_path, vendoring, capsys
):
    out = tmp_path / "out"
    code = homelab_render.cluster(out, ["speedtest"], None, vendored=vendoring)
    assert code == RENDER_FAILED
    assert "must not substitute" in capsys.readouterr().err
    assert not (out / "cluster").exists()


def test_a_stub_render_writes_the_vendored_files(tmp_path, vendoring):
    stubs = tmp_path / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(stubs)]) == 0
    out = tmp_path / "out"
    code = homelab_render.cluster(out, ["littlelink"], stubs, vendored=vendoring)
    assert code == RENDER_OK
    assert _vendored_into(out / "cluster" / "littlelink")


def test_both_round_trip_sides_carry_the_vendored_files(tmp_path, vendoring):
    keys = homelab_render.secret_keys()
    passes = stub_passes(shadow_diff.stub_vars(), keys, frozenset())[:1]
    sides = render_sides(
        tmp_path / "rt", ["littlelink"], keys, passes, vendored=vendoring
    )
    assert _vendored_into(sides.cluster("placeholder") / "littlelink")
    assert _vendored_into(sides.cluster(passes[0].name) / "littlelink")
