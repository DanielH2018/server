"""Every rendered k8s object has a vendored schema to be checked against.

`validate/k8s_manifests.py` reports an object with no schema as skipped and lets it through, so
a manifest in a new API group, or a new CRD kind, would arrive checked by nothing. This guard
fails instead, and names what to vendor. The schemas themselves are refreshed by
`scripts/validate/refresh_vendored_schemas.py`.

Run: uv run pytest ansible/tests/k8s/test_rendered_kinds_have_vendored_schemas.py
"""

from _k8s_render import rendered_docs
from validate.validate_lib.k8s_schema import NO_SCHEMA, schema_error

# Kinds the census must reach: one per vendored core group the tree uses, plus each CRD. A
# render that stopped yielding them would otherwise pass this guard over nothing.
MUST_FIND = frozenset(
    {
        ("v1", "Service"),
        ("apps/v1", "Deployment"),
        ("batch/v1", "CronJob"),
        ("discovery.k8s.io/v1", "EndpointSlice"),
        ("networking.k8s.io/v1", "NetworkPolicy"),
        ("rbac.authorization.k8s.io/v1", "Role"),
        ("storage.k8s.io/v1", "StorageClass"),
        ("traefik.io/v1alpha1", "IngressRoute"),
    }
)


def unschemaed_kinds(docs) -> set[tuple[str, str]]:
    """(apiVersion, kind) of every object `schema_error` has no schema for."""
    return {
        (doc.get("apiVersion"), doc.get("kind"))
        for doc in docs
        if schema_error(doc) is NO_SCHEMA
    }


def test_the_census_reaches_every_named_kind():
    seen = {(doc.get("apiVersion"), doc.get("kind")) for _, _, doc in rendered_docs()}
    assert MUST_FIND <= seen, f"the render no longer yields {sorted(MUST_FIND - seen)}"


def test_every_rendered_kind_has_a_vendored_schema():
    missing = unschemaed_kinds(doc for _, _, doc in rendered_docs())
    assert not missing, (
        f"no vendored schema for {sorted(missing)}. Add the group's file to CORE_GROUP_FILES, "
        "or the CRD to VENDORED, in scripts/validate/refresh_vendored_schemas.py, and run it."
    )


def test_a_kind_from_an_unvendored_group_is_flagged():
    doc = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "x"},
    }
    assert unschemaed_kinds([doc]) == {("autoscaling/v2", "HorizontalPodAutoscaler")}
