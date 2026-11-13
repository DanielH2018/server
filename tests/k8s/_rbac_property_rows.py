"""Rendered-manifest property rows about who a role binds to which RBAC role.

`test_rendered_properties.py` runs these rows beside its own. A row lives here only to keep that
file under the test-module length cap; the rules for what makes a row are in its docstring.
"""

from lib.repo_paths import K8S_ROLES

from _helpers import load_yaml
from _property_table import Property

_CLUSTER_BASE = K8S_ROLES / "cluster-base"
_DEFAULTS = load_yaml(_CLUSTER_BASE / "defaults" / "main.yml")
READONLY_SA = _DEFAULTS["cluster_base_readonly_sa_name"]
READONLY_NS = _DEFAULTS["cluster_base_readonly_sa_namespace"]

# Every group Kubernetes puts the identity in by name. `system:authenticated` is left out: it is
# the same for every credential, so a binding to it is not a binding of this identity.
READONLY_GROUPS = frozenset(
    {"system:serviceaccounts", f"system:serviceaccounts:{READONLY_NS}"}
)

# What cluster-base's own render binds the identity to, plus the built-in `view` it binds
# already. A role named here is read-only; a binding from any other role to a roleRef outside
# these is a new grant to the identity.
ALLOWED_CLUSTER_ROLES = frozenset({"view", READONLY_SA})
ALLOWED_ROLES = frozenset({(READONLY_NS, f"{READONLY_SA}-kcm-lease")})

BINDING_KINDS = frozenset({"ClusterRoleBinding", "RoleBinding"})


def _names_the_identity(binding: dict) -> bool:
    namespace = binding["metadata"].get("namespace")
    for subject in binding.get("subjects") or []:
        if subject.get("kind") == "Group" and subject.get("name") in READONLY_GROUPS:
            return True
        if (
            subject.get("kind") == "ServiceAccount"
            and subject.get("name") == READONLY_SA
            and (subject.get("namespace") or namespace) == READONLY_NS
        ):
            return True
    return False


def _readonly_identity_bindings(role: str, tpl: str, doc: dict):
    if doc.get("kind") in BINDING_KINDS and _names_the_identity(doc):
        yield f"{role}:{doc['kind']}/{doc['metadata']['name']}", doc


def _readonly_binding_offence(role: str, doc: dict) -> str | None:
    ref = doc["roleRef"]
    if ref["kind"] == "ClusterRole" and ref["name"] in ALLOWED_CLUSTER_ROLES:
        return None
    if (
        ref["kind"] == "Role"
        and (
            doc["metadata"].get("namespace"),
            ref["name"],
        )
        in ALLOWED_ROLES
    ):
        return None
    return f"binds the read-only identity to {ref['kind']} {ref['name']!r}"


def binding_doc(
    kind: str, name: str, ref_kind: str, ref_name: str, subjects, namespace=None
):
    metadata = {"name": name, **({"namespace": namespace} if namespace else {})}
    return {
        "kind": kind,
        "metadata": metadata,
        "roleRef": {"kind": ref_kind, "name": ref_name},
        "subjects": subjects,
    }


def _identity_subject() -> dict:
    return {"kind": "ServiceAccount", "name": READONLY_SA, "namespace": READONLY_NS}


RBAC_PROPERTIES = (
    Property(
        name="read-only-identity-is-bound-only-to-read-roles",
        reason=(
            "Plain `kubectl` in a session runs as the read-only ServiceAccount that cluster-base "
            "defines, and the rule that Flux and `promote` are the only write path to the "
            "cluster holds only while that identity's roleRefs stay read-only. "
            "test_k8s_manifests_rbac.py audits the bindings that cluster-base and headlamp "
            "render, and test_aggregated_clusterroles.py refuses a binding to `edit` or "
            "`admin`. Neither reads a binding that another role renders for this identity, "
            "whether it names the ServiceAccount or a `system:serviceaccounts` group that "
            "contains it, to a role that writes. This row reads every rendered binding."
        ),
        select=_readonly_identity_bindings,
        offence=_readonly_binding_offence,
        red=(
            "rogue-role",
            binding_doc(
                "ClusterRoleBinding",
                "rogue-admin",
                "ClusterRole",
                "cluster-admin",
                [_identity_subject()],
            ),
        ),
        more_red=(
            (
                "rogue-role",
                binding_doc(
                    "ClusterRoleBinding",
                    "rogue-group",
                    "ClusterRole",
                    "edit",
                    [{"kind": "Group", "name": "system:serviceaccounts"}],
                ),
            ),
            (
                "rogue-role",
                binding_doc(
                    "ClusterRoleBinding",
                    "rogue-namespace-group",
                    "ClusterRole",
                    "admin",
                    [
                        {
                            "kind": "Group",
                            "name": f"system:serviceaccounts:{READONLY_NS}",
                        }
                    ],
                ),
            ),
            (
                "rogue-role",
                binding_doc(
                    "RoleBinding",
                    "rogue-local",
                    "ClusterRole",
                    "edit",
                    [{"kind": "ServiceAccount", "name": READONLY_SA}],
                    namespace=READONLY_NS,
                ),
            ),
            (
                "rogue-role",
                binding_doc(
                    "RoleBinding",
                    "rogue-lease",
                    "Role",
                    f"{READONLY_SA}-kcm-lease",
                    [_identity_subject()],
                    namespace="fake-namespace",
                ),
            ),
        ),
        green=(
            READONLY_SA,
            binding_doc(
                "ClusterRoleBinding",
                "ok-view",
                "ClusterRole",
                "view",
                [_identity_subject()],
            ),
        ),
        more_green=(
            (
                READONLY_SA,
                binding_doc(
                    "RoleBinding",
                    "ok-lease",
                    "Role",
                    f"{READONLY_SA}-kcm-lease",
                    [_identity_subject()],
                    namespace=READONLY_NS,
                ),
            ),
        ),
        # Three bindings render, all cluster-base's: the identity's `view` and own ClusterRole
        # bindings, and the lease Role binding. A floor of three and the names below fail when
        # the census stops seeing them.
        min_matches=3,
        must_find=frozenset(
            {
                f"cluster-base:ClusterRoleBinding/{READONLY_SA}-view",
                f"cluster-base:ClusterRoleBinding/{READONLY_SA}",
                f"cluster-base:RoleBinding/{READONLY_SA}-kcm-lease",
            }
        ),
    ),
)
