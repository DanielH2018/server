"""Every role that takes snapshots restores its own replicas, or a revert leaves it at zero."""

from lib import yaml_fast
from _k8s_render import rendered_docs
from _volume_revert import _REPO


def _snapshot_roles() -> dict[str, list[str]]:
    """Every role that opts into a pre-deploy snapshot, and the claims it declares."""
    roles = {}
    for defaults in sorted((_REPO / "ansible/roles/k8s").glob("*/defaults/main.yml")):
        declared = (yaml_fast.safe_load(defaults.read_text()) or {}).get(
            "k8s_autodeploy_snapshot_pvcs"
        )
        if declared:
            roles[defaults.parents[1].name] = declared
    return roles


def test_every_snapshot_role_restores_its_own_replicas() -> None:
    """`volume-revert` scales to zero and never scales back.

    That is only correct because the apply which follows restores the workload, and it restores it
    only if the role renders a Deployment **named for the service** with an **explicit replica
    count**.

    Nothing else in the repo enforces that. A role that opts into a snapshot but runs a StatefulSet,
    names its workload something other than the service, or omits `replicas:` and inherits the API
    server's default would leave its service at zero replicas after a rollback — the outage the
    rollback was meant to end. This is the executable version of a sentence that otherwise lives
    only in a comment.
    """
    roles = _snapshot_roles()
    # A floor, and only as a sanity check on the reader itself: the guard below is the loop,
    # which covers however many roles opt in.
    assert len(roles) >= 13, (
        f"only found {len(roles)} snapshot roles; the reader is broken"
    )

    deployments: dict[str, list[dict]] = {}
    for role, _tpl, doc in rendered_docs():
        if role in roles and doc.get("kind") == "Deployment":
            deployments.setdefault(role, []).append(doc)

    for role in sorted(roles):
        named = [
            doc
            for doc in deployments.get(role, [])
            if doc.get("metadata", {}).get("name") == role
        ]
        assert named, (
            f"{role} declares k8s_autodeploy_snapshot_pvcs but renders no Deployment named "
            f"{role!r}. k8s/volume-revert scales `deployment/{role}` to zero and does not scale "
            f"it back, so this service would stay down after a rollback."
        )
        for doc in named:
            replicas = doc.get("spec", {}).get("replicas")
            assert isinstance(replicas, int) and replicas >= 1, (
                f"{role}'s Deployment has replicas={replicas!r}. It must be an explicit count "
                f"of at least one: the apply after a revert is what brings the workload back."
            )
