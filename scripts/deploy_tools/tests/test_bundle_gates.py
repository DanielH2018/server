"""`promote.py check-bundle`'s source, host and no-stub gates, each a clean and flagged pair.

Every fixture bundle publishes bento-pdf with a copy of its rendered routes
(`_bundle_fixtures.py`), against a fixture inventory and a fixture `secrets.yml` that holds
the key names only, as SOPS leaves them.

Run: uv run pytest scripts/deploy_tools/tests/test_bundle_gates.py
"""

from collections.abc import Mapping

import pytest
from _bundle_fixtures import bundle, route

import bundle_gates
from renderer.renderer_lib.bundle import write_bundle

DOMAIN = "example.test"
CONFIGMAP = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  namespace: homelab\ndata:\n  value: {value}\n"
CLUSTER_VARS = """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
secretGenerator:
- name: homelab-cluster-vars
  namespace: flux-system
"""


@pytest.fixture
def inputs(tmp_path):
    """The fixture `secrets.yml` and `group_vars/all.yml`, as gate keyword arguments."""
    secrets = tmp_path / "secrets.yml"
    secrets.write_text("domain: ENC[x]\nemail: ENC[x]\nsops:\n  version: 3\n")
    all_vars = tmp_path / "all.yml"
    all_vars.write_text(f"domain: {DOMAIN}\n")
    return {"secrets": secrets, "all_vars": all_vars}


def _gates(tmp_path, inputs, files, published=("bento-pdf",), kinds=()):
    out = tmp_path / "out"
    write_bundle(files, out)
    return bundle_gates.gates(out, list(published), kinds=kinds, **inputs)


def _bento(extra: Mapping[str, str] | None = None, host_domain: str = DOMAIN, **kw):
    return bundle(
        {"bento-pdf": {"ingressroute.yaml": route(host_domain), **(extra or {})}}, **kw
    )


def test_the_bento_pdf_bundle_is_clean(tmp_path, inputs):
    assert _gates(tmp_path, inputs, _bento()) == []


def test_master_s_empty_bundle_is_clean(tmp_path, inputs):
    files = bundle({})
    files["digests.json"] = "{}\n"
    assert _gates(tmp_path, inputs, files, published=()) == []


# -- source ----------------------------------------------------------------------------------


def test_a_shell_reference_in_a_tag_without_post_build_is_clean(tmp_path, inputs):
    extra = {"configmap.yaml": CONFIGMAP.format(value='"${HOME}/x and $${email}"')}
    assert _gates(tmp_path, inputs, _bento(extra)) == []


def test_a_secret_key_reference_in_a_tag_without_post_build_is_flagged(
    tmp_path, inputs
):
    extra = {"configmap.yaml": CONFIGMAP.format(value='"${email}"')}
    assert _gates(tmp_path, inputs, _bento(extra)) == [
        "source: cluster/bento-pdf/configmap.yaml holds ${email}, a key of secrets.yml, "
        "and the Kustomization bento-pdf in flux/bento-pdf.yaml has no postBuild"
    ]


def test_a_substituting_tag_beside_the_cluster_vars_kind_is_clean(tmp_path, inputs):
    files = _bento(
        {"configmap.yaml": CONFIGMAP.format(value='"${email}"')},
        substitutes={"bento-pdf"},
    )
    files |= bundle({"cluster-vars": {}})
    files["cluster/cluster-vars/kustomization.yaml"] = CLUSTER_VARS
    files["flux/kustomization.yaml"] = bundle({"bento-pdf": {}, "cluster-vars": {}})[
        "flux/kustomization.yaml"
    ]
    assert _gates(tmp_path, inputs, files, kinds={"cluster-vars"}) == []


def test_a_substituting_tag_with_no_cluster_vars_kind_is_flagged(tmp_path, inputs):
    files = _bento(
        {"configmap.yaml": CONFIGMAP.format(value='"${email}"')},
        substitutes={"bento-pdf"},
    )
    assert _gates(tmp_path, inputs, files) == [
        "source: the Kustomization bento-pdf in flux/bento-pdf.yaml substitutes from "
        "Secret/homelab-cluster-vars, which no secretGenerator in the bundle makes"
    ]


def _deferring(substitutes=frozenset()):
    """bento-pdf with a deferred directory holding `${email}`: the second document of
    `flux/bento-pdf.yaml` is `bento-pdf-deferred`, judged against its own directory."""
    deferred = {"configmap.yaml": CONFIGMAP.format(value='"${email}"')}
    return bundle(
        {
            "bento-pdf": {"ingressroute.yaml": route(DOMAIN)},
            "bento-pdf-deferred": deferred,
        },
        substitutes=substitutes,
    )


def test_a_deferred_document_with_post_build_beside_the_cluster_vars_kind_is_clean(
    tmp_path, inputs
):
    files = _deferring(substitutes={"bento-pdf-deferred"})
    files |= bundle({"cluster-vars": {}})
    files["cluster/cluster-vars/kustomization.yaml"] = CLUSTER_VARS
    files["flux/kustomization.yaml"] = bundle({"bento-pdf": {}, "cluster-vars": {}})[
        "flux/kustomization.yaml"
    ]
    assert _gates(tmp_path, inputs, files, kinds={"cluster-vars"}) == []


def test_a_deferred_document_substituting_from_an_ungenerated_secret_is_flagged(
    tmp_path, inputs
):
    """The red half of the clean case above: the same bundle without the cluster-vars kind,
    so only the deferred document's `substituteFrom` can fail it."""
    assert _gates(tmp_path, inputs, _deferring(substitutes={"bento-pdf-deferred"})) == [
        "source: the Kustomization bento-pdf-deferred in flux/bento-pdf.yaml substitutes "
        "from Secret/homelab-cluster-vars, which no secretGenerator in the bundle makes"
    ]


def test_a_deferred_document_without_post_build_over_a_secret_key_is_flagged(
    tmp_path, inputs
):
    """#5172: the gate read only the file's first document and walked only `cluster/<tag>/`."""
    assert _gates(tmp_path, inputs, _deferring()) == [
        "source: cluster/bento-pdf-deferred/configmap.yaml holds ${email}, a key of "
        "secrets.yml, and the Kustomization bento-pdf-deferred in flux/bento-pdf.yaml has "
        "no postBuild"
    ]


def test_a_document_applying_a_path_outside_cluster_is_flagged(tmp_path, inputs):
    files = _bento()
    files["flux/bento-pdf.yaml"] = files["flux/bento-pdf.yaml"].replace(
        "path: ./cluster/bento-pdf", "path: ./cluster/../flux"
    )
    assert _gates(tmp_path, inputs, files) == [
        "source: could not examine the Kustomization bento-pdf in flux/bento-pdf.yaml: it "
        "applies './cluster/../flux', which is no directory under cluster/"
    ]


def test_a_published_tag_the_source_gate_cannot_examine_is_flagged(tmp_path, inputs):
    found = _gates(tmp_path, inputs, _bento(), published=("bento-pdf", "sonarr"))
    assert found == [
        "source: could not examine sonarr: it needs flux/sonarr.yaml and cluster/sonarr/"
    ]


# -- host ------------------------------------------------------------------------------------


def test_a_route_that_ends_in_the_inventory_domain_is_clean(tmp_path, inputs):
    out = tmp_path / "out"
    write_bundle(_bento(), out)
    assert bundle_gates.host(out, ["bento-pdf"], DOMAIN) == []


@pytest.mark.parametrize(
    "host_domain",
    ["stub-domain", "example.org", "${domain}"],
    ids=["a stub-mode render", "another literal host", "an unsubstituted placeholder"],
)
def test_a_route_that_ends_in_anything_else_is_flagged(tmp_path, inputs, host_domain):
    out = tmp_path / "out"
    write_bundle(_bento(host_domain=host_domain), out)
    found = bundle_gates.host(out, ["bento-pdf"], DOMAIN)
    assert (
        "host: IngressRoute/homelab/bento-pdf in cluster/bento-pdf/ingressroute.yaml routes "
        f"'bento-pdf.local.{host_domain}', which does not end in .{DOMAIN}"
    ) in found


def test_a_published_bento_pdf_with_no_local_route_is_flagged(tmp_path, inputs):
    files = bundle({"bento-pdf": {"configmap.yaml": CONFIGMAP.format(value="x")}})
    assert _gates(tmp_path, inputs, files) == [
        "host: IngressRoute/homelab/bento-pdf, the gate's named member while bento-pdf is "
        f"published, holds no Host rule ending in .local.{DOMAIN}"
    ]


# -- no-stub ---------------------------------------------------------------------------------


def test_a_value_that_only_starts_like_a_stub_is_clean(tmp_path, inputs):
    extra = {"configmap.yaml": CONFIGMAP.format(value="stub-domain-name")}
    assert _gates(tmp_path, inputs, _bento(extra)) == []


def test_a_stub_value_in_a_configmap_is_flagged(tmp_path, inputs):
    extra = {"configmap.yaml": CONFIGMAP.format(value="stub-domain")}
    assert _gates(tmp_path, inputs, _bento(extra)) == [
        "no-stub: cluster/bento-pdf/configmap.yaml holds the stub value stub-domain"
    ]


def test_a_secrets_file_with_no_key_fails_every_gate(tmp_path, inputs):
    inputs["secrets"].write_text("sops:\n  version: 3\n")
    assert _gates(tmp_path, inputs, _bento()) == [
        f"gates: {inputs['secrets']} names no key"
    ]
