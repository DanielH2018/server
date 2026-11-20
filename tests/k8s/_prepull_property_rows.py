"""Property rows about `<tag>-prepull`, over one Flux cluster render of every role that opts in.

A role that sets `flux_prepull_timeout` gets a `<tag>-prepull` Kustomization whose Jobs pull
the role's images onto its node before `<tag>` applies, so a `Recreate` Deployment's old pod
serves through the pull (`scripts/renderer/renderer_lib/prepull.py`). The shared render that
`rendered_docs()` serves holds no Flux bundle, so these rows read `prepull_bundles()` instead:
one `homelab_render.cluster` render with every such role published under Flux, shared by the
whole run. Each subject is one tag's bundle, because the image check joins a Job to the
workloads it pulls for.

The expected images and placement come from the workloads' own pod specs, not from
`prepull.py`'s helpers, so a renderer that copied the wrong field cannot agree with itself.

`test_rendered_properties.py` runs these rows beside its own.
"""

import json
import tempfile
from pathlib import Path

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from lib.k8s_roles import k8s_entries
from _helpers import K8S_ROLES, load_defaults
from _k8s_render import pod_spec, rendered_docs
from _property_table import Property
from render_share import shared_value
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE
from renderer.renderer_lib.ordering import Ordering, prepull_timeouts

_WORKLOADS = frozenset({"Deployment", "StatefulSet"})
_HOSTNAME = "kubernetes.io/hostname"
SHARE_KEY = ["k8s_prepull_flux_bundles"]


def _docs(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [d for d in yaml_fast.safe_load_all(path.read_text()) if isinstance(d, dict)]


def _bundle(out: Path, tag: str) -> dict:
    named = load_defaults(K8S_ROLES / tag).get("flux_prepull_workloads")
    workloads = {
        d["metadata"]["name"]: pod_spec(d)
        for path in sorted((out / "cluster" / tag).glob("*.yaml"))
        for d in _docs(path)
        if d.get("kind") in _WORKLOADS
        and (named is None or d["metadata"]["name"] in named)
    }
    return {
        "kustomizations": {
            d["metadata"]["name"]: d["spec"]
            for d in _docs(out / "flux" / f"{tag}.yaml")
        },
        "jobs": [
            pod_spec(d)
            for path in sorted((out / "cluster" / f"{tag}-prepull").glob("*.yaml"))
            for d in _docs(path)
            if d.get("kind") == "Job"
        ],
        "workloads": workloads,
    }


def _render() -> tuple[tuple[str, str, dict], ...]:
    tags = sorted(prepull_timeouts(k8s_entries())[0])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cluster_file = root / CLUSTER_VARS_FILE
        cluster_file.write_text("")
        out = root / "out"
        code = homelab_render.cluster(
            out,
            tags,
            None,
            flux_owned=lambda: set(tags),
            cluster_file=cluster_file,
            ordering=lambda o: Ordering(flux_owned=frozenset(o)),
        )
        assert code == RENDER_OK, f"the Flux render of {tags} exited {code}"
        return tuple((tag, f"flux/{tag}.yaml", _bundle(out, tag)) for tag in tags)


def prepull_bundles():
    """(tag, flux file, bundle) for each role that sets `flux_prepull_timeout`, rendered once.

    A bundle holds the tag's Flux Kustomizations by name, the pod specs of its `<tag>-prepull`
    Jobs, and the pod specs of the workloads those Jobs pull for, by name. A role's
    `flux_prepull_workloads` narrows the workloads to the ones it names.
    """
    return iter(shared_value(SHARE_KEY, _render))


def _prepull_kustomization(role: str, tpl: str, bundle: dict):
    spec = bundle["kustomizations"].get(f"{role}-prepull")
    if spec is not None:
        yield role, spec


def _timeout_offence(role: str, spec: dict) -> str | None:
    want = load_defaults(K8S_ROLES / role)["flux_prepull_timeout"]
    if spec.get("timeout") == want:
        return None
    return f"timeout is {spec.get('timeout')!r}, not the role's flux_prepull_timeout {want!r}"


def _role_kustomization(role: str, tpl: str, bundle: dict):
    if f"{role}-prepull" in bundle["kustomizations"]:
        yield role, bundle["kustomizations"].get(role, {})


def _depends_offence(role: str, spec: dict) -> str | None:
    if {"name": f"{role}-prepull"} in spec.get("dependsOn", []):
        return None
    return f"{role} does not depend on {role}-prepull, so it can apply before the pull"


def _images(spec: dict) -> set[str]:
    containers = (spec.get("initContainers") or []) + (spec.get("containers") or [])
    return {c["image"] for c in containers}


def _placement(spec: dict) -> tuple:
    node_affinity = (spec.get("affinity") or {}).get("nodeAffinity")
    return tuple(
        json.dumps(value, sort_keys=True)
        for value in (spec.get("nodeSelector"), node_affinity, spec.get("tolerations"))
    )


def _prepull_jobs(role: str, tpl: str, bundle: dict):
    if f"{role}-prepull" in bundle["kustomizations"]:
        yield role, {"jobs": bundle["jobs"], "workloads": bundle["workloads"]}


def _pull_offence(role: str, subject: dict) -> str | None:
    if not subject["workloads"]:
        return "the role renders no workload for its prepull to pull for"
    wanted: dict[tuple, set[str]] = {}
    for spec in subject["workloads"].values():
        wanted.setdefault(_placement(spec), set()).update(_images(spec))
    pulled: dict[tuple, set[str]] = {}
    for spec in subject["jobs"]:
        pulled.setdefault(_placement(spec), set()).update(_images(spec))
    if pulled == wanted:
        return None
    return (
        f"the Jobs pull {sorted(map(sorted, pulled.values()))} under {len(pulled)} "
        f"placement(s), but the workloads run {sorted(map(sorted, wanted.values()))} "
        f"under {len(wanted)}"
    )


def _required_hostnames(spec: dict) -> list[str]:
    affinity = (spec.get("affinity") or {}).get("nodeAffinity") or {}
    required = affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    return [
        value
        for term in required.get("nodeSelectorTerms") or []
        for expr in term.get("matchExpressions") or []
        if expr.get("key") == _HOSTNAME
        for value in expr.get("values") or []
    ]


def _media_pv_nodes() -> list[str]:
    claim = load_defaults(K8S_ROLES / "tdarr")["tdarr_k8s_media_claim"]
    for _role, _tpl, doc in rendered_docs():
        if doc["kind"] == "PersistentVolume" and (
            (doc["spec"].get("claimRef") or {}).get("name") == claim
        ):
            terms = doc["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"]
            return [
                v for t in terms for e in t["matchExpressions"] for v in e["values"]
            ]
    return []


def _tdarr_jobs(role: str, tpl: str, bundle: dict):
    if role == "tdarr":
        yield (
            "tdarr",
            {
                "jobs": [_required_hostnames(job) for job in bundle["jobs"]],
                "pv": _media_pv_nodes(),
            },
        )


def _media_node_offence(role: str, subject: dict) -> str | None:
    if subject["pv"] and all(nodes == subject["pv"] for nodes in subject["jobs"]):
        return None
    return (
        f"the prepull Jobs require nodes {subject['jobs']}, not the node "
        f"{subject['pv']} the media-data PV pins tdarr to"
    )


def _job(images: list[str], node: str = "daniel-box") -> dict:
    return {
        "affinity": {"nodeAffinity": _node_affinity(node)},
        "containers": [{"image": i} for i in images],
    }


def _node_affinity(node: str) -> dict:
    expr = {"key": _HOSTNAME, "operator": "In", "values": [node]}
    term = {"nodeSelectorTerms": [{"matchExpressions": [expr]}]}
    return {"requiredDuringSchedulingIgnoredDuringExecution": term}


def _pod(
    images: list[str], init: tuple[str, ...] = (), node: str = "daniel-box"
) -> dict:
    return {**_job(images, node), "initContainers": [{"image": i} for i in init]}


# The seven roles the per-role prepull test files covered before they became these rows.
_COVERED = frozenset(
    {
        "crowdsec",
        "home-assistant",
        "jellyfin",
        "karakeep",
        "observability",
        "tdarr",
        "valheim",
    }
)

PREPULL_PROPERTIES = (
    Property(
        name="prepull-kustomization-takes-the-roles-timeout",
        reason=(
            "The `<tag>-prepull` Kustomization's timeout is how long Flux waits for the pull "
            "before it gives up on the revision. A cold pull of tdarr's 1432 MiB image took "
            "25m37s (#3689), so the role sizes `flux_prepull_timeout` to its own image; a "
            "bundle that fell back to the default timeout would fail every slow pull."
        ),
        source=prepull_bundles,
        select=_prepull_kustomization,
        offence=_timeout_offence,
        red=("tdarr", {"timeout": "600s"}),
        green=("tdarr", {"timeout": "3000s"}),
        more_red=(("tdarr", {}),),
        # 10 roles set flux_prepull_timeout. The floor is the count.
        min_matches=10,
        must_find=_COVERED,
    ),
    Property(
        name="role-depends-on-its-prepull",
        reason=(
            "A `Recreate` Deployment stops its old pod before the kubelet pulls the new image. "
            "On 2026-10-19 a karakeep digest bump pulled from GHCR for 16.5 minutes and "
            "karakeep served nothing meanwhile (#5778). `<tag>` depending on `<tag>-prepull` "
            "is what makes the old pod serve through the pull; without the edge the two "
            "Kustomizations apply in parallel and the pre-pull buys nothing."
        ),
        source=prepull_bundles,
        select=_role_kustomization,
        offence=_depends_offence,
        red=("tdarr", {"dependsOn": [{"name": "media-volume"}]}),
        green=("tdarr", {"dependsOn": [{"name": "tdarr-prepull"}]}),
        more_red=(("tdarr", {}),),
        min_matches=10,
        must_find=_COVERED,
    ),
    Property(
        name="prepull-jobs-pull-the-workloads-images-where-they-run",
        reason=(
            "A pre-pull helps only if it puts every image the workloads run, init containers "
            "included, onto the node the workloads will land on. The renderer copies each "
            "workload's nodeSelector, node affinity and tolerations onto one Job per distinct "
            "placement. This row recomputes both from the workloads' pod specs and requires "
            "the Jobs to match placement by placement. A role's `flux_prepull_workloads` "
            "narrows the set: observability pulls for Grafana alone, because loki, tempo and "
            "kube-state-metrics pin no node and carry no `sh` (#5875)."
        ),
        source=prepull_bundles,
        select=_prepull_jobs,
        offence=_pull_offence,
        red=(
            "karakeep",
            {
                "jobs": [_job(["app"])],
                "workloads": {"karakeep": _pod(["app"], init=("init",))},
            },
        ),
        green=(
            "karakeep",
            {
                "jobs": [_job(["init", "app"])],
                "workloads": {"karakeep": _pod(["app"], init=("init",))},
            },
        ),
        more_red=(
            (
                "karakeep",
                {
                    "jobs": [_job(["app"], node="daniel-server")],
                    "workloads": {"karakeep": _pod(["app"])},
                },
            ),
            ("karakeep", {"jobs": [], "workloads": {}}),
            ("karakeep", {"jobs": [], "workloads": {"karakeep": _pod(["app"])}}),
        ),
        more_green=(
            (
                "karakeep",
                {
                    "jobs": [_job(["a"]), _job(["b"], node="daniel-server")],
                    "workloads": {
                        "one": _pod(["a"]),
                        "two": _pod(["b"], node="daniel-server"),
                    },
                },
            ),
        ),
        min_matches=10,
        must_find=_COVERED,
    ),
    Property(
        name="tdarr-prepull-lands-on-the-media-node",
        reason=(
            "tdarr mounts `media-data`, a local PV that media-volume pins to one node, so "
            "tdarr's pod can only run there and its pre-pull is wasted anywhere else. The Job "
            "copies the Deployment's placement, which is why the Deployment carries a required "
            "node affinity to `k8s_primary_node`. This row ties the rendered Job to the PV's "
            "own node; test_media_volume_pins_tdarrs_claim.py holds the PV's pin."
        ),
        source=prepull_bundles,
        select=_tdarr_jobs,
        offence=_media_node_offence,
        red=("tdarr", {"jobs": [["daniel-server"]], "pv": ["daniel-box"]}),
        green=("tdarr", {"jobs": [["daniel-box"]], "pv": ["daniel-box"]}),
        more_red=(("tdarr", {"jobs": [[]], "pv": []}),),
        must_find=frozenset({"tdarr"}),
    ),
)
