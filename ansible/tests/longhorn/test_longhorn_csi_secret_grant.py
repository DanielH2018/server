#!/usr/bin/env python3
"""The `longhorn` render must not give the CSI service account cluster-wide Secret access.

Longhorn v1.13.0's `deploy/longhorn.yaml` binds `longhorn-csi-service-account` to
`ClusterRole/longhorn-csi-secret-role`, which grants `get` on every Secret in the cluster. It is
a compatibility default for StorageClasses that carry a `csi.storage.k8s.io/*-secret-*`
parameter, and neither class here does (#5969). Two `$patch: delete` patches under
`files/patches/` drop the role and its binding. The renderer lists every file there under the
Kustomization's `patches:`.

`_secret_grants` is the check. It removes each object a delete patch names from upstream's
objects, then reports every surviving ClusterRoleBinding that hands the CSI service account a
ClusterRole reading Secrets. It reads the grant's meaning, so a release that renames the role
or the binding still fails here. `NAMED` is the member it must find in upstream's file alone,
which proves the check sees the grant before the patches remove it.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_csi_secret_grant.py
"""

from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

LONGHORN_FILES = K8S_ROLES / "longhorn" / "files"
UPSTREAM = LONGHORN_FILES / "upstream" / "longhorn.yaml"
PATCHES = LONGHORN_FILES / "patches"
CSI_ACCOUNT = ("longhorn-system", "longhorn-csi-service-account")
NAMED = "longhorn-csi-secret-bind"
DELETED = {
    ("ClusterRoleBinding", "longhorn-csi-secret-bind"),
    ("ClusterRole", "longhorn-csi-secret-role"),
}


def _docs(text: str) -> list[dict]:
    return [d for d in yaml_fast.safe_load_all(text) if isinstance(d, dict)]


def _patch_docs() -> list[dict]:
    return [
        doc
        for path in sorted(PATCHES.glob("*.y*ml"))
        for doc in _docs(path.read_text())
    ]


def _deleted(patches: list[dict]) -> set[tuple[str, str]]:
    """The (kind, name) of every object a `$patch: delete` patch removes."""
    return {
        (doc["kind"], doc["metadata"]["name"])
        for doc in patches
        if doc.get("$patch") == "delete"
    }


def _reads_secrets(role: dict) -> bool:
    return any(
        "" in rule.get("apiGroups", [])
        and {"secrets", "*"} & set(rule.get("resources", []))
        and {"get", "list", "watch", "*"} & set(rule.get("verbs", []))
        for rule in role.get("rules") or []
    )


def _secret_grants(upstream: list[dict], patches: list[dict]) -> list[str]:
    """Every surviving ClusterRoleBinding giving the CSI account a Secret-reading ClusterRole."""
    gone = _deleted(patches)
    live = [d for d in upstream if (d["kind"], d["metadata"]["name"]) not in gone]
    reading = {
        d["metadata"]["name"]
        for d in live
        if d["kind"] == "ClusterRole" and _reads_secrets(d)
    }
    return sorted(
        d["metadata"]["name"]
        for d in live
        if d["kind"] == "ClusterRoleBinding"
        and d["roleRef"]["kind"] == "ClusterRole"
        and d["roleRef"]["name"] in reading
        and any(
            (s.get("namespace"), s.get("name")) == CSI_ACCOUNT
            and s.get("kind") == "ServiceAccount"
            for s in d.get("subjects") or []
        )
    )


def test_upstreams_secret_grant_is_flagged():
    assert NAMED in _secret_grants(_docs(UPSTREAM.read_text()), [])


def test_the_patched_render_is_clean():
    # fact: ansible/roles/k8s/longhorn/CLAUDE.md#At a glance
    assert _secret_grants(_docs(UPSTREAM.read_text()), _patch_docs()) == []


def test_the_render_drops_both_upstream_objects():
    assert DELETED <= _deleted(_patch_docs())
