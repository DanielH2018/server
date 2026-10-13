"""The flux role's vendored install: its header, its digests, its fence and its rollouts.

`roles/k8s/flux/files/gotk-components.yaml` is generated, never edited, and
`templates/components.yaml.j2` fits it to the fleet guards through the `flux_components`
filter. Each property here is one the fleet guards cannot see:

- the header names the `flux_version` the file was generated at, so a Renovate bump of the
  version alone fails here rather than deploying the old release under a new name;
- every controller image is pinned by digest, which the fleet digest rule reads from `*_image`
  defaults only;
- no NetworkPolicy in `flux-system` admits every namespace, which the two dropped upstream
  policies did;
- a changed Secret render rolls no controller, because each `flux_controllers` entry declares
  `restart_on: []` and no controller pod reads the notification Secrets;
- `flux_sync_suspend` renders `suspend: true` or no `suspend` key, never `false`;
- the `verify-drill` OCIRepository renders only with its lever on, and is no Discord source;
- the OCIRepository admits exactly the signer `promote.py` verifies.

Run: uv run pytest ansible/tests/k8s/test_flux_role.py
"""

import copy
import re

import forge
import pytest
from ansible.errors import AnsibleFilterError

from _helpers import ROLES, load_defaults, render_expr
from lib import yaml_fast
from _k8s_render import render_role_template, rendered_docs
from _release_expectation import include_role_vars, rollouts
from flux_components import FilterModule
from test_volume_names_descriptive import volume_names

FLUX = ROLES / "k8s" / "flux"
VENDORED = FLUX / "files" / "gotk-components.yaml"
CONTROLLERS = frozenset(
    {"source-controller", "kustomize-controller", "notification-controller"}
)
_DIGEST_PINNED = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")


def _flux_docs() -> list[dict]:
    return [doc for role, _tpl, doc in rendered_docs() if role == "flux"]


def _flux_version() -> str:
    return load_defaults(FLUX)["flux_version"]


# --- The header names the version -------------------------------------------------------


def header_names_version(text: str, version: str) -> bool:
    """Whether the vendored file was generated at `version`, by every mark it carries.

    Line 1, the pipeline, is hand-copied, so a bump that edited it and skipped the regeneration
    would still match it. `flux install` itself writes `# Flux Version: <v>` and stamps
    `app.kubernetes.io/version: <v>` on every object, so those must name `version` too.
    """
    first = text.split("\n", 1)[0]
    stamped = set(re.findall(r"^\s+app\.kubernetes\.io/version: (\S+)$", text, re.M))
    return (
        first.startswith("# flux install --export ")
        and f" --version={version} " in first
        and re.findall(r"^# Flux Version: (\S+)$", text, re.M) == [version]
        and stamped == {version}
    )


def test_header_names_version_is_clean_on_the_vendored_file():
    assert header_names_version(VENDORED.read_text(), _flux_version())


def test_header_names_version_is_flagged_on_a_bump_that_skipped_regeneration():
    """The red half: Renovate rewrote `flux_version`, and the file still says the old one."""
    stale = VENDORED.read_text().replace(" --version=v", " --version=old-v", 1)
    assert not header_names_version(stale, _flux_version())


def test_header_names_version_is_flagged_on_a_hand_edited_pipeline_line():
    """The red half the pipeline line alone misses: line 1 says the new release, and the body
    flux generated still says the old one."""
    version = _flux_version()
    first, body = VENDORED.read_text().split("\n", 1)
    edited = first.replace(f"--version={version}", "--version=v99.0.0") + "\n" + body
    assert " --version=v99.0.0 " in edited.split("\n", 1)[0]
    assert not header_names_version(edited, "v99.0.0")


# --- The images are pinned by digest ----------------------------------------------------


def unpinned_images(docs: list[dict]) -> dict[str, list[str]]:
    """{Deployment name: [image]} for each container image not pinned as `<repo>:<tag>@<digest>`."""
    found = {}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        pod = doc["spec"]["template"]["spec"]
        images = [c["image"] for c in pod.get("initContainers", []) + pod["containers"]]
        found[doc["metadata"]["name"]] = [
            i for i in images if not _DIGEST_PINNED.match(i)
        ]
    return found


def test_unpinned_images_is_clean_on_every_flux_controller():
    found = unpinned_images(_flux_docs())
    assert set(found) == CONTROLLERS, f"the census found {sorted(found)}"
    assert not any(found.values()), found


def test_unpinned_images_is_flagged_on_a_tag_only_image():
    docs = copy.deepcopy(_flux_docs())
    deployment = next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "kustomize-controller"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    container["image"] = container["image"].split("@", 1)[0]
    assert unpinned_images(docs)["kustomize-controller"] == [container["image"]]


# --- No policy in flux-system admits every namespace ------------------------------------


def all_namespace_peers(docs: list[dict], namespace: str) -> list[str]:
    """Names of the NetworkPolicies in `namespace` with a peer that matches every namespace.

    An empty `namespaceSelector` selects every namespace, whatever pod selector sits beside it.
    """
    found = []
    for doc in docs:
        if doc.get("kind") != "NetworkPolicy":
            continue
        if doc["metadata"].get("namespace") != namespace:
            continue
        for rule in doc["spec"].get("ingress") or []:
            for peer in rule.get("from") or []:
                selector = peer.get("namespaceSelector")
                if selector is not None and not (
                    selector.get("matchLabels") or selector.get("matchExpressions")
                ):
                    found.append(doc["metadata"]["name"])
    return found


# The two upstream policies the vendoring filter drops, as `flux install --network-policy=true`
# emits them at v2.9.6.
_UPSTREAM_DROPPED = """
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: allow-scraping, namespace: flux-system}
spec:
  ingress:
  - from:
    - namespaceSelector: {}
    ports: [{port: 8080, protocol: TCP}]
  podSelector: {}
  policyTypes: [Ingress]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: allow-webhooks, namespace: flux-system}
spec:
  ingress:
  - from:
    - namespaceSelector: {}
    ports: [{port: 9292, protocol: TCP}]
  podSelector: {matchLabels: {app: notification-controller}}
  policyTypes: [Ingress]
"""


def test_all_namespace_peers_is_clean_on_the_rendered_fence():
    docs = _flux_docs()
    policies = {d["metadata"]["name"] for d in docs if d["kind"] == "NetworkPolicy"}
    assert {"allow-egress", "flux-allow-prometheus"} <= policies, policies
    assert all_namespace_peers(docs, "flux-system") == []


def test_all_namespace_peers_is_flagged_on_the_unfiltered_install():
    docs = _flux_docs() + list(yaml_fast.safe_load_all(_UPSTREAM_DROPPED))
    assert all_namespace_peers(docs, "flux-system") == [
        "allow-scraping",
        "allow-webhooks",
    ]


# --- A changed Secret render rolls no controller ----------------------------------------


def _declared_self_rollouts(entries: list[dict]) -> list[dict]:
    """The include's `manifests_self_rollouts`, rendered over the role's defaults."""
    declared = include_role_vars(FLUX)["manifests_self_rollouts"]
    return render_expr(declared, **{**load_defaults(FLUX), "flux_controllers": entries})


def _flux_rollouts(entries: list[dict]) -> dict:
    return rollouts(
        manifests_service="flux",
        manifests_rollout="",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=_declared_self_rollouts(entries),
        manifests_render={"changed": True},
        manifests_secret_render={"changed": True},
        manifests_apply={"changed": False, "stdout": ""},
    )


def test_a_changed_secret_render_expects_no_controller_to_roll():
    entries = load_defaults(FLUX)["flux_controllers"]
    # The fingerprints read each workload in its declared namespace, which defaults to
    # k8s_namespace; read there, every controller would read "not rolled".
    declared = _declared_self_rollouts(entries)
    assert {e["namespace"] for e in declared} == {"flux-system"}, declared
    recorded = _flux_rollouts(entries)
    assert set(recorded) == CONTROLLERS, recorded
    assert not any(r["restart"] for r in recorded.values()), recorded


def test_without_restart_on_a_changed_secret_render_expects_every_controller_to_roll():
    """The red half: the shared default `[config, secret, image]` stamps all three, and no
    task restarts them, so `probe.py health flux` would read each as NOT ROLLED."""
    entries = [
        {k: v for k, v in e.items() if k != "restart_on"}
        for e in load_defaults(FLUX)["flux_controllers"]
    ]
    recorded = _flux_rollouts(entries)
    assert all(r["restart"] for r in recorded.values()), recorded


# --- The filter refuses what it cannot fit ----------------------------------------------

_DEPLOYMENT = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: source-controller}
spec:
  template:
    spec:
      containers:
      - name: manager
        image: x
        volumeMounts: [{mountPath: /cache, name: cache}]
      volumes: [{emptyDir: {}, name: cache}]
"""


@pytest.mark.parametrize(
    "text",
    [_DEPLOYMENT, "apiVersion: v1\nkind: Namespace\nmetadata: {name: flux-system}\n"],
    ids=["unnamed-volume", "no-deployment"],
)
def test_flux_components_refuses_an_install_it_cannot_fit(text):
    with pytest.raises((ValueError, AnsibleFilterError)) as caught:
        FilterModule().filters()["flux_components"](text, {}, "homelab-critical", {})
    assert isinstance(caught.value, AnsibleFilterError), type(caught.value)
    assert str(caught.value).startswith("flux_components: "), str(caught.value)


def test_the_volume_name_guard_reads_the_renamed_volumes():
    """The volume-name guard scans rendered `- name:` lines under `volumes:`. A sequence
    written at its key's own column reads to it as the end of the block, so it collected no
    name from this filter's first output and passed while checking nothing."""
    out = FilterModule().filters()["flux_components"](
        _DEPLOYMENT, {}, "homelab-critical", {"cache": "cache"}
    )
    assert volume_names(out) == {
        "volumes": {"source-controller-cache"},
        "volumeMounts": {"source-controller-cache"},
    }, out


# --- The suspend lever renders only `true`, never `false` -------------------------------

_SUSPENDABLE = {("OCIRepository", "homelab"), ("Kustomization", "homelab")}


def _sync_docs(**overrides) -> list[dict]:
    text = render_role_template("flux", "sync.yaml.j2", overrides)
    return [d for d in yaml_fast.safe_load_all(text) if isinstance(d, dict)]


def suspend_offences(docs: list[dict], lever: bool) -> list[str]:
    """`<kind>/<name>` for each suspendable object whose `spec.suspend` the lever does not explain.

    Lever on: both objects must carry `suspend: true`. Lever off: neither may carry the key at
    all, because the client-side apply would turn an explicit `false` into an undo of an
    operator's hand suspend. A suspendable object missing from the render is an offence too,
    so a renamed object cannot pass by absence.
    """
    found = {
        (d["kind"], d["metadata"]["name"]): d["spec"]
        for d in docs
        if (d["kind"], d["metadata"]["name"]) in _SUSPENDABLE
    }
    offences = [f"{k}/{n} missing" for k, n in sorted(_SUSPENDABLE - set(found))]
    for (kind, name), spec in sorted(found.items()):
        if lever and spec.get("suspend") is not True:
            offences.append(f"{kind}/{name} suspend={spec.get('suspend', '<absent>')}")
        if not lever and "suspend" in spec:
            offences.append(f"{kind}/{name} suspend={spec['suspend']}")
    return offences


def test_suspend_offences_is_clean_with_the_lever_off():
    assert suspend_offences(_sync_docs(flux_sync_suspend=False), lever=False) == []


def test_suspend_offences_is_clean_with_the_lever_on():
    assert suspend_offences(_sync_docs(flux_sync_suspend=True), lever=True) == []


def test_suspend_offences_is_flagged_on_an_explicit_false():
    """The red half: `suspend: false` with the lever off undoes a hand suspend."""
    docs = copy.deepcopy(_sync_docs(flux_sync_suspend=False))
    for doc in docs:
        if (doc["kind"], doc["metadata"]["name"]) in _SUSPENDABLE:
            doc["spec"]["suspend"] = False
    assert suspend_offences(docs, lever=False) == [
        "Kustomization/homelab suspend=False",
        "OCIRepository/homelab suspend=False",
    ]


# --- The verify drill is a lever, and never a Discord source -----------------------------


def _names(docs: list[dict], kind: str) -> set[str]:
    return {d["metadata"]["name"] for d in docs if d["kind"] == kind}


def test_verify_drill_renders_only_while_its_lever_is_on():
    assert _names(_sync_docs(flux_verify_drill=False), "OCIRepository") == {"homelab"}
    assert _names(_sync_docs(flux_verify_drill=True), "OCIRepository") == {
        "homelab",
        "verify-drill",
    }


def test_deploy_errors_names_no_drill_source():
    docs = _sync_docs(flux_verify_drill=True)
    alert = next(
        d
        for d in docs
        if d["kind"] == "Alert" and d["metadata"]["name"] == "deploy-errors"
    )
    sources = {(s["kind"], s["name"]) for s in alert["spec"]["eventSources"]}
    assert sources == {("Kustomization", "*"), ("OCIRepository", "homelab")}, sources


# --- The cluster and `promote.py` agree on what counts as signed -------------------------


def identity_admits(docs: list[dict], subject: str, issuer: str) -> bool:
    """Whether the OCIRepository `homelab` accepts a keyless signature from `subject`/`issuer`.

    source-controller matches each field as a Go regular expression; these patterns use only
    syntax that Python's `re` reads the same way.
    """
    repo = next(
        d
        for d in docs
        if d["kind"] == "OCIRepository" and d["metadata"]["name"] == "homelab"
    )
    assert repo["spec"]["verify"]["provider"] == "cosign", repo["spec"]["verify"]
    return any(
        re.search(m["subject"], subject) and re.search(m["issuer"], issuer)
        for m in repo["spec"]["verify"]["matchOIDCIdentity"]
    )


def test_identity_admits_the_signer_promote_verifies():
    assert identity_admits(_sync_docs(), forge.SIGNER_IDENTITY, forge.SIGNER_ISSUER)


@pytest.mark.parametrize(
    "subject",
    [
        forge.SIGNER_IDENTITY.replace("promote.yml", "ci.yml"),
        forge.SIGNER_IDENTITY.replace("refs/heads/master", "refs/heads/feature"),
        forge.SIGNER_IDENTITY + "x",
        forge.SIGNER_IDENTITY.replace("github.com/", "githubXcom/"),
    ],
    ids=["other-workflow", "other-ref", "suffix", "unescaped-dot"],
)
def test_identity_admits_is_flagged_on_another_signer(subject):
    assert not identity_admits(_sync_docs(), subject, forge.SIGNER_ISSUER)


# --- This PR's Secrets roll no controller -------------------------------------------------


def pod_secret_refs(docs: list[dict]) -> dict[str, set[str]]:
    """{Deployment name: Secret names its pod spec reads through env, envFrom or a volume}."""
    found = {}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        pod = doc["spec"]["template"]["spec"]
        names = {
            v["secret"]["secretName"] for v in pod.get("volumes", []) if "secret" in v
        }
        for c in pod.get("initContainers", []) + pod["containers"]:
            names |= {
                e["secretRef"]["name"] for e in c.get("envFrom", []) if "secretRef" in e
            }
            names |= {
                e["valueFrom"]["secretKeyRef"]["name"]
                for e in c.get("env", [])
                if "secretKeyRef" in e.get("valueFrom", {})
            }
        found[doc["metadata"]["name"]] = names
    return found


def _rendered_secret_names() -> set[str]:
    return {
        d["metadata"]["name"]
        for role, tpl, d in rendered_docs()
        if role == "flux" and tpl == "secret.yaml.j2" and d["kind"] == "Secret"
    }


def test_no_controller_reads_the_notification_secrets_through_its_pod_spec():
    """`secret.yaml.j2` moves the secret render on every rotation, and `restart_on: []` then
    expects no roll. That holds only while no controller pod reads these Secrets, because
    notification-controller reads them through the API on each event."""
    secrets = _rendered_secret_names()
    assert secrets == {"flux-github-status", "flux-discord"}, secrets
    refs = pod_secret_refs(_flux_docs())
    assert set(refs) == CONTROLLERS, refs
    assert not any(names & secrets for names in refs.values()), refs
    recorded = _flux_rollouts(load_defaults(FLUX)["flux_controllers"])
    assert not any(r["restart"] for r in recorded.values()), recorded


def test_pod_secret_refs_is_flagged_on_an_envfrom_secret():
    docs = copy.deepcopy(_flux_docs())
    deployment = next(
        d
        for d in docs
        if d["kind"] == "Deployment"
        and d["metadata"]["name"] == "notification-controller"
    )
    deployment["spec"]["template"]["spec"]["containers"][0].setdefault(
        "envFrom", []
    ).append({"secretRef": {"name": "flux-discord"}})
    assert pod_secret_refs(docs)["notification-controller"] == {"flux-discord"}
