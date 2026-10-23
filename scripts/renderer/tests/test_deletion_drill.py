"""The deletion drill's bundles and verdicts (D6.11, `deletion_drill.py`).

Every verdict is a `_is_clean` / `_is_flagged` pair on fixture JSON, so a verdict that stopped
matching fails its own test. The bundle tests hold the drill to the renderer's writers: the
marker and the Kustomization come from `prune_label.label` and `bundle.flux_files`, and the two
red halves are stripped on revision 1 only.

Run: uv run pytest scripts/renderer/tests/test_deletion_drill.py
"""

import re

import pytest
from lib import yaml_fast
from renderer import deletion_drill as drill
from renderer.renderer_lib.prune_label import PRUNE_LABEL

IMAGE = "example/pause@sha256:" + "0" * 64
FAKE_KEY = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"


def _rev(*, keep_pvcs: bool) -> dict[str, str]:
    tags = drill.TAGS if keep_pvcs else (drill.KEEP,)
    return drill.bundle(tags, IMAGE, keep_pvcs=keep_pvcs)


# --- The bundles ---------------------------------------------------------------------


def test_revision_1_holds_all_three_tags_with_the_marker_on_every_claim_but_two():
    files = _rev(keep_pvcs=True)
    assert {p for p in files if p.startswith("flux/")} == {
        "flux/kustomization.yaml",
        *(f"flux/{t}.yaml" for t in drill.TAGS),
    }
    claims = {
        p: PRUNE_LABEL in text
        for p, text in files.items()
        if p.rsplit("/", 1)[-1].startswith("pvc")
    }
    unmarked = {p for p, marked in claims.items() if not marked}
    assert unmarked == {
        f"cluster/{drill.RED}/pvc.yaml",
        f"cluster/{drill.KEEP}/pvc-keep-unmarked.yaml",
    }
    assert len(claims) == 4


def test_a_namespace_carries_the_marker_in_every_tag():
    files = _rev(keep_pvcs=True)
    for tag in drill.TAGS:
        assert PRUNE_LABEL in files[f"cluster/{tag}/namespace.yaml"]


def test_only_the_red_tag_loses_its_deletion_policy():
    files = _rev(keep_pvcs=True)
    assert drill.rendered_policy(files, drill.AS_RENDERED) == "Orphan"
    assert drill.rendered_policy(files, drill.KEEP) == "Orphan"
    assert drill.rendered_policy(files, drill.RED) == "default"


def test_the_image_token_is_replaced_in_every_deployment():
    files = _rev(keep_pvcs=True)
    deployments = [t for p, t in files.items() if "deployment" in p]
    assert len(deployments) == 4
    assert all(IMAGE in t and drill.IMAGE_TOKEN not in t for t in deployments)


def test_revision_2_drops_the_two_tags_and_every_claim_of_the_kept_one():
    files = _rev(keep_pvcs=False)
    tags_held = {
        p.split("/")[1].removesuffix(".yaml")
        for p in files
        if p.count("/") > 1 or "kustomization" not in p
    }
    assert tags_held == {drill.KEEP}
    assert not any(p.rsplit("/", 1)[-1].startswith("pvc") for p in files)
    index = yaml_fast.safe_load(files["flux/kustomization.yaml"])
    assert index["resources"] == [f"{drill.KEEP}.yaml"]


def test_a_directory_kustomization_lists_every_file_but_itself():
    files = drill.bundle(
        (drill.KEEP,),
        IMAGE,
        keep_pvcs=False,
        extra=drill.marker_configmap(drill.BG_MARKER),
    )
    listed = yaml_fast.safe_load(files[f"cluster/{drill.KEEP}/kustomization.yaml"])
    assert f"{drill.BG_MARKER}.yaml" in listed["resources"]
    assert "kustomization.yaml" not in listed["resources"]


def test_strip_marker_removes_the_marker_and_keeps_the_other_label():
    marked = drill.label(
        (drill.FIXTURES / drill.RED / "pvc.yaml").read_text(), "pvc.yaml"
    )
    assert PRUNE_LABEL in marked
    stripped = yaml_fast.safe_load(drill.strip_marker(marked))
    assert stripped["metadata"]["labels"] == {"app": "data"}


# --- The break-glass documents, from the flux role's own templates --------------------


def test_the_break_glass_source_verifies_against_the_secret_and_follows_the_registry():
    source, secret = drill.break_glass_documents("kind-registry:5000", "bg", FAKE_KEY)
    spec = source["spec"]
    assert spec["url"] == "oci://kind-registry:5000/bg"
    assert spec["insecure"] is True
    assert spec["ref"] == {"tag": "bg"}
    assert spec["verify"] == {
        "provider": "cosign",
        "secretRef": {"name": drill.BG_SECRET},
    }
    assert secret["stringData"] == {"cosign.pub": FAKE_KEY}


def test_the_production_root_is_the_roles_root():
    root = drill.production_root()
    assert root["spec"]["path"] == "./flux"
    assert root["spec"]["sourceRef"]["name"] == "homelab"


def test_the_drill_source_has_no_verify_and_reads_plain_http():
    spec = drill.drill_source("kind-registry:5000")["spec"]
    assert "verify" not in spec
    assert spec["insecure"] is True


# --- The verdicts --------------------------------------------------------------------


def _meta(name, namespace=None, **extra):
    meta = {"name": name, **extra}
    if namespace:
        meta["namespace"] = namespace
    return meta


def _ks(name, *, ready="True", applied="", message=""):
    return {
        "kind": "Kustomization",
        "metadata": _meta(name, drill.FLUX_NAMESPACE),
        "status": {
            "lastAppliedRevision": applied,
            "conditions": [{"type": "Ready", "status": ready, "message": message}],
        },
    }


def _pvc(name, namespace, *, phase="Bound", terminating=False):
    extra = {"deletionTimestamp": "2026-10-23T00:00:00Z"} if terminating else {}
    return {
        "kind": "PersistentVolumeClaim",
        "metadata": _meta(name, namespace, **extra),
        "status": {"phase": phase},
    }


def _obj(kind, name, namespace=None, terminating=False):
    extra = {"deletionTimestamp": "2026-10-23T00:00:00Z"} if terminating else {}
    return {"kind": kind, "metadata": _meta(name, namespace, **extra)}


def _source(revision, *, ready="True", verified="True", message="", generation=1):
    conditions = [
        {
            "type": "Ready",
            "status": ready,
            "message": message,
            "observedGeneration": generation,
        }
    ]
    if verified:
        conditions.append({"type": "SourceVerified", "status": verified})
    return {
        "kind": "OCIRepository",
        "metadata": _meta(drill.ROOT, drill.FLUX_NAMESPACE, generation=generation),
        "status": {"artifact": {"revision": revision}, "conditions": conditions},
    }


def _revision_1() -> list[dict]:
    return [
        *(_ks(t) for t in drill.TAGS),
        _pvc("data", drill.AS_RENDERED),
        _pvc("data", drill.RED),
        _pvc(drill.KEEP_MARKED, drill.KEEP),
        _pvc(drill.KEEP_UNMARKED, drill.KEEP),
    ]


def _verdict(problems: list[str]) -> bool:
    return not problems


def test_revision_1_is_clean_with_every_tag_ready_and_every_claim_bound():
    assert _verdict(drill.revision_1_problems(_revision_1()))


def test_revision_1_is_flagged_on_an_unready_tag_and_on_a_pending_claim():
    items = _revision_1()
    items[0] = _ks(drill.AS_RENDERED, ready="False", message="health check failed")
    items[6] = _pvc(drill.KEEP_UNMARKED, drill.KEEP, phase="Pending")
    problems = drill.revision_1_problems(items)
    assert len(problems) == 2
    assert "not Ready" in problems[0] and "not Bound" in problems[1]


def _as_rendered(*, deployment: bool, claim=True, terminating=False) -> list[dict]:
    items = [_obj("Namespace", drill.AS_RENDERED)]
    if claim:
        items.append(_pvc("data", drill.AS_RENDERED, terminating=terminating))
    if deployment:
        items.append(_obj("Deployment", "holder", drill.AS_RENDERED))
    return items


def test_as_rendered_is_clean_under_orphan_with_the_deployment_kept():
    assert _verdict(drill.as_rendered_problems(_as_rendered(deployment=True), "Orphan"))


def test_as_rendered_is_clean_under_the_default_with_the_deployment_gone():
    items = _as_rendered(deployment=False)
    assert _verdict(drill.as_rendered_problems(items, "default"))


def test_as_rendered_is_flagged_when_the_policy_and_the_deployment_disagree():
    assert drill.as_rendered_problems(_as_rendered(deployment=False), "Orphan")
    assert drill.as_rendered_problems(_as_rendered(deployment=True), "default")


def test_as_rendered_is_flagged_on_a_lost_claim_and_on_a_terminating_one():
    assert drill.as_rendered_problems(
        _as_rendered(deployment=True, claim=False), "Orphan"
    )
    items = _as_rendered(deployment=True, terminating=True)
    assert drill.as_rendered_problems(items, "Orphan")


def test_as_rendered_is_flagged_on_a_surviving_kustomization():
    items = [*_as_rendered(deployment=True), _ks(drill.AS_RENDERED)]
    assert drill.as_rendered_problems(items, "Orphan")


def test_red_half_is_clean_on_a_gone_or_terminating_claim():
    assert _verdict(drill.red_half_problems([]))
    assert _verdict(
        drill.red_half_problems([_pvc("data", drill.RED, terminating=True)])
    )


def test_red_half_is_flagged_on_a_claim_that_survived():
    problems = drill.red_half_problems([_pvc("data", drill.RED)])
    assert problems and "cannot see a lost one" in problems[0]


def _keep(
    *, marked=True, unmarked_state="gone", applied="drill@sha256:2"
) -> list[dict]:
    items = [
        _source("drill@sha256:2"),
        _ks(drill.KEEP, applied=applied),
    ]
    if marked:
        items.append(_pvc(drill.KEEP_MARKED, drill.KEEP))
    if unmarked_state == "terminating":
        items.append(_pvc(drill.KEEP_UNMARKED, drill.KEEP, terminating=True))
    if unmarked_state == "alive":
        items.append(_pvc(drill.KEEP_UNMARKED, drill.KEEP))
    return items


def test_inventory_prune_is_clean_when_only_the_unmarked_claim_is_lost():
    assert _verdict(drill.inventory_prune_problems(_keep()))
    assert _verdict(drill.inventory_prune_problems(_keep(unmarked_state="terminating")))


@pytest.mark.parametrize(
    "items",
    [
        _keep(marked=False),
        _keep(unmarked_state="alive"),
        _keep(applied="drill@sha256:1"),
    ],
    ids=["marked claim lost", "unmarked claim kept", "revision 2 not applied"],
)
def test_inventory_prune_is_flagged(items):
    assert drill.inventory_prune_problems(items)


def _green(**kw) -> list[dict]:
    revision = kw.pop("revision", "bg@sha256:3")
    items = [
        _source(revision, **kw),
        _ks(drill.KEEP, applied=revision),
        _obj("ConfigMap", drill.BG_MARKER, drill.KEEP),
    ]
    return items


def test_break_glass_is_clean_on_a_verified_applied_bundle():
    assert _verdict(drill.break_glass_problems(_green()))


@pytest.mark.parametrize(
    "items",
    [
        _green(ready="False", message="failed to verify"),
        _green(verified=""),
        _green(revision="drill@sha256:2"),
        _green()[:2],
    ],
    ids=["not ready", "never verified", "wrong bundle", "marker not applied"],
)
def test_break_glass_is_flagged(items):
    assert drill.break_glass_problems(items)


def _red(**kw) -> list[dict]:
    message = kw.pop("message", "failed to verify the signature of the artifact")
    served = kw.pop("served", "bg@sha256:3")
    return [_source(served, ready="False", verified="", message=message, **kw)]


def test_break_glass_red_is_clean_on_a_refusal_that_names_verification():
    assert _verdict(drill.break_glass_red_problems(_red()))


def test_break_glass_red_is_flagged_on_a_transport_failure():
    items = _red(message="http: server gave HTTP response to HTTPS client")
    problems = drill.break_glass_red_problems(items)
    assert problems and "not on verification" in problems[0]


def test_break_glass_red_is_flagged_when_the_red_bundle_was_accepted():
    ready = [
        _source("bg-red@sha256:4"),
        _obj("ConfigMap", drill.BG_RED_MARKER, drill.KEEP),
    ]
    problems = drill.break_glass_red_problems(ready)
    assert len(problems) >= 3


def test_break_glass_red_is_flagged_before_the_source_judged_the_new_spec():
    items = _red()
    items[0]["metadata"]["generation"] = 2
    assert "not yet judged" in drill.break_glass_red_problems(items)[0]


def test_break_glass_red_is_flagged_when_the_green_bundle_is_not_served():
    assert drill.break_glass_red_problems(_red(served=""))


def test_every_case_name_has_a_verdict():
    assert set(drill.CASES) == {
        "revision-1",
        "as-rendered",
        "red-half",
        "inventory-prune",
        "break-glass",
        "break-glass-red",
    }
    assert re.search(drill.VERIFICATION_WORDS, "Verification failed")
