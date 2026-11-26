"""The packageRules table's harness, and its rows for the CI, host and lockstep pins.

`test_renovate_package_rules.py` runs every row; its docstring says what a row asserts. The
rows for the k8s and Pi image pins sit there, because one module of both would exceed the
module-length cap.
"""

import copy
from collections.abc import Callable
from typing import NamedTuple


from _renovate import _PACKAGE_RULES, _resolve_setting

Rules = list[dict]
Mutation = Callable[[Rules], Rules]

ANNOTATED = "annotated-pin"
GRA = "github-release-attachments"
UPDATE_TYPES = ("major", "minor", "patch", "digest", "pinDigest")


class Dep(NamedTuple):
    """A dependency as Renovate extracts it.

    Attributes:
      name: The depName.
      file: The repo-relative file the pin sits in, which `matchFileNames` is matched against.
      datasource: The datasource the manager reports.
      dep_type: The manager's depType, which `matchDepTypes` reads.
      manager: The manager that extracts the dep.
      tag: For a k8s `_image:` pin, text its tag must hold; `""` checks only that the pin is
        still in `file`. None for a dep that is not an `_image:` pin.
    """

    name: str
    file: str
    datasource: str
    dep_type: str | None = None
    manager: str = "custom.regex"
    tag: str | None = None


class Has:
    """An expected string value, given as the substrings it must contain."""

    def __init__(self, *texts: str) -> None:
        self.texts = texts

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and all(t in other for t in self.texts)

    def __repr__(self) -> str:
        return f"Has{self.texts!r}"


class Row(NamedTuple):
    id: str
    dep: Dep
    update_type: str
    field: str
    expected: object
    rules: Mutation | None = None
    shares: str | None = None


def _rule(rules: Rules, prefix: str) -> int:
    """The index of the one rule whose description starts with `prefix`."""
    found = [i for i, r in enumerate(rules) if r["description"].startswith(prefix)]
    assert len(found) == 1, f"{len(found)} packageRules describe {prefix!r}"
    return found[0]


def moved(prefix: str, before: str | None = None) -> Mutation:
    """The rule moved to just before the `before` rule, or to the end when None."""

    def mutate(rules: Rules) -> Rules:
        rule = rules.pop(_rule(rules, prefix))
        rules.insert(len(rules) if before is None else _rule(rules, before), rule)
        return rules

    return mutate


def _only(prefix: str) -> Mutation:
    return lambda rules: [rules[_rule(rules, prefix)]]


def without(*prefixes: str) -> Mutation:
    def mutate(rules: Rules) -> Rules:
        drop = {_rule(rules, p) for p in prefixes}
        return [r for i, r in enumerate(rules) if i not in drop]

    return mutate


def edited(prefix: str, edit: Callable[[dict], None]) -> Mutation:
    def mutate(rules: Rules) -> Rules:
        edit(rules[_rule(rules, prefix)])
        return rules

    return mutate


def appended(rule: dict) -> Mutation:
    return lambda rules: [*rules, rule]


def resolve(row: Row) -> object:
    rules = _PACKAGE_RULES
    if row.rules is not None:
        rules = row.rules(copy.deepcopy(_PACKAGE_RULES))
    dep = row.dep
    return _resolve_setting(
        row.field,
        dep.name,
        dep.file,
        row.update_type,
        dep.datasource,
        rules,
        dep_type=dep.dep_type,
        manager=dep.manager,
    )


CI = ".github/workflows/ci.yml"
PROMOTE = ".github/workflows/promote.yml"
HOMELAB_TOOLS = "hosts/packages/homelab-tools/package.yml"
PI_COMPOSE = "hosts/components/pi-compose/services"
PI_DOCKER = "hosts/components/pi-docker/component.yml"


def k8s_image(name: str, role: str, tag: str = "") -> Dep:
    return Dep(name, f"k8s/roles/{role}/defaults/main.yml", "docker", tag=tag)


def _annotated(name: str, file: str) -> Dep:
    return Dep(name, file, "github-releases", ANNOTATED)


# kind: the binary and the node image move in one PR, and the node image takes no minor.
KIND = Dep("kubernetes-sigs/kind", CI, GRA)
KIND_NODE = Dep("kindest/node", CI, "docker")
KIND_GROUP = "kind (binary + node image)"

# cosign decides the signature format Flux's source-controller verifies.
COSIGN = [
    Dep("sigstore/cosign", f".github/workflows/{w}.yml", GRA)
    for w in ("promote", "prune-artifacts", "flux-deletion-drill")
]
COSIGN_INSTALLER = Dep("sigstore/cosign-installer", PROMOTE, GRA)
COSIGN_RULE = "The cosign release that promote.yml"

# Every dep named fluxcd/flux2 lands in the one manual flux group.
FLUX = [
    Dep("fluxcd/flux2", PROMOTE, GRA),
    _annotated("fluxcd/flux2", "k8s/roles/flux/defaults/main.yml"),
    Dep("fluxcd/flux2", HOMELAB_TOOLS, GRA),
]

# The Pi's Docker engine packages never join the automerging catch-all.
DOCKER_ENGINE = [
    Dep(p, PI_DOCKER, "deb")
    for p in (
        "containerd.io",
        "docker-ce",
        "docker-compose-plugin",
        "docker-buildx-plugin",
    )
]
DOCKER_ENGINE_RULE = "The Pi's Docker engine packages"

# The annotated-pin rule finishes a pin no per-package rule names, and yields to one that does.
VENDOR_TOOL = _annotated("vendor/tool", "hosts/components/k3s-server/component.yml")
CROWDSEC_PLUGIN = _annotated(
    "maxlerebourg/crowdsec-bouncer-traefik-plugin",
    "k8s/roles/traefik/defaults/main.yml",
)
ANNOTATED_RULE = "Every `# renovate:` annotated pin"
NODE_EXPORTER_IMAGE = k8s_image("prom/node-exporter", "node-exporter")
NODE_EXPORTER_PI = Dep("prometheus/node_exporter", HOMELAB_TOOLS, GRA)
NODE_EXPORTER_RULE = "node_exporter is pinned twice"

# Merging the MetalLB or Longhorn pin is the upgrade, so each names the gate land.sh runs.
K3S = _annotated("k3s-io/k3s", "k8s/roles/system-upgrade/defaults/main.yml")
METALLB = _annotated("metallb/metallb", "k8s/roles/metallb/defaults/main.yml")
LONGHORN = _annotated("longhorn/longhorn", "k8s/roles/longhorn/defaults/main.yml")
CLUSTER_PINS = (K3S, METALLB, LONGHORN)
CLUSTER_GATED = (
    (METALLB, "MetalLB (manual —", "metallb-upgrade"),
    (LONGHORN, "Longhorn (manual —", "longhorn-upgrade"),
)

# Every site that reports the repo's Python to Renovate moves in one PR.
PYTHON_CI = Dep("python", ".github/actions/setup/action.yml", "python-version")
PYTHON_PYENV = Dep("python", ".python-version", "docker", manager="pyenv")
PYTHON_K8S = [
    Dep("python", "k8s/roles/monitor-bridge/defaults/main.yml", "docker"),
    Dep("python", "k8s/roles/prowlarr/defaults/main.yml", "docker"),
]
PYTHON_SITES = [
    PYTHON_CI,
    PYTHON_PYENV,
    *(
        Dep(
            "python",
            f"k8s/roles/{r}/templates/Dockerfile.j2",
            "docker",
            manager="dockerfile",
        )
        for r in ("ical-proxy", "homelab-mcp")
    ),
    *PYTHON_K8S,
]
PYTHON_RULE = "Every Python pin the repo keeps in lockstep"


def _drop_file(path: str) -> Callable[[dict], None]:
    return lambda rule: rule["matchFileNames"].remove(path)


HOST_ROWS = [
    Row("kind-binary-group", KIND, "minor", "groupName", KIND_GROUP),
    Row("kind-node-patch-group", KIND_NODE, "patch", "groupName", KIND_GROUP),
    Row("kind-node-digest-group", KIND_NODE, "digest", "groupName", KIND_GROUP),
    Row(
        "kind-rule-hoisted-splits-the-group",
        KIND,
        "minor",
        "groupName",
        "{{depName}}",
        rules=moved("The two kind pins", before="Bundle minor and patch image bumps"),
    ),
    *(
        Row(f"kind-node-{u}-enabled", KIND_NODE, u, "enabled", value)
        for u, value in (
            ("minor", False),
            ("major", False),
            ("patch", None),
            ("digest", None),
        )
    ),
    *(
        Row(f"cosign-{d.file}-{u}-{field}", d, u, field, value, shares=shares)
        for d in COSIGN
        for u in UPDATE_TYPES
        for field, value, shares in (
            ("automerge", False, None),
            ("pinDigests", False, None),
            ("groupName", Has("cosign (manual"), "cosign"),
        )
    ),
    Row(
        "cosign-rule-alone-holds-cosign",
        COSIGN[0],
        "patch",
        "automerge",
        False,
        rules=_only(COSIGN_RULE),
    ),
    Row(
        "cosign-rule-alone-skips-the-installer",
        COSIGN_INSTALLER,
        "patch",
        "automerge",
        None,
        rules=_only(COSIGN_RULE),
    ),
    *(
        Row(f"flux-{d.file}-{u}-{field}", d, u, field, value, shares=shares)
        for d in FLUX
        for u in UPDATE_TYPES
        for field, value, shares in (
            ("groupName", Has("flux (manual"), "flux"),
            ("automerge", False, None),
        )
    ),
    *(
        Row(f"docker-engine-{d.name}-{field}", d, "minor", field, value)
        for d in DOCKER_ENGINE
        for field, value in (
            ("groupName", Has("docker engine on the Pi")),
            ("automerge", False),
        )
    ),
    Row(
        "docker-engine-rule-dropped-joins-the-catch-all",
        DOCKER_ENGINE[0],
        "minor",
        "groupName",
        "container images (non-major)",
        rules=without(DOCKER_ENGINE_RULE),
    ),
    Row(
        "annotated-pin-names-the-refresh-command",
        VENDOR_TOOL,
        "minor",
        "groupName",
        Has("asset_pins.py --refresh {{depName}}"),
    ),
    Row("annotated-pin-never-automerges", VENDOR_TOOL, "minor", "automerge", False),
    Row(
        "crowdsec-plugin-keeps-the-denylist-marker",
        CROWDSEC_PLUGIN,
        "minor",
        "groupName",
        Has("k8s_autodeploy: false"),
    ),
    Row(
        "annotated-rule-moved-last-strips-the-marker",
        CROWDSEC_PLUGIN,
        "minor",
        "groupName",
        Has("asset_pins.py --refresh"),
        rules=moved(ANNOTATED_RULE),
    ),
    *(
        Row(f"node-exporter-{d.name}-{field}", d, "minor", field, value, shares=shares)
        for d in (NODE_EXPORTER_IMAGE, NODE_EXPORTER_PI)
        for field, value, shares in (
            (
                "groupName",
                Has("(manual", "homelab-tools node-exporter pin"),
                "node_exporter",
            ),
            ("automerge", False, None),
        )
    ),
    Row(
        "node-exporter-rule-before-homelab-tools-splits-the-twins",
        NODE_EXPORTER_PI,
        "minor",
        "groupName",
        "{{depName}}",
        rules=moved(NODE_EXPORTER_RULE, before="CI toolchain and docs-lint pins"),
    ),
    Row(
        "node-exporter-image-digest-automerges",
        NODE_EXPORTER_IMAGE,
        "digest",
        "automerge",
        True,
    ),
    *(
        Row(f"cluster-{d.name}-automerge", d, "minor", "automerge", False)
        for d in CLUSTER_PINS
    ),
    Row(
        "cluster-k3s-group",
        K3S,
        "minor",
        "groupName",
        Has("k3s control plane (manual —", "docs/k3s-upgrade.md"),
    ),
    *(
        Row(f"cluster-{d.name}-{field}", d, "minor", field, value)
        for d, name, gate in CLUSTER_GATED
        for field, value in (
            ("groupName", Has(name, gate)),
            ("description", Has("land.sh", "homelab/upgrade-gates")),
        )
    ),
    Row(
        "cluster-k3s-rule-description-names-h6",
        K3S,
        "minor",
        "description",
        Has("H6"),
        rules=without("k3s stays on the minor"),
    ),
    Row(
        "coredns-image-in-a-k8s-role-is-disabled",
        Dep(
            "rancher/mirrored-coredns-coredns",
            "k8s/roles/coredns/defaults/main.yml",
            "docker",
            ANNOTATED,
        ),
        "minor",
        "enabled",
        False,
    ),
    Row(
        "coredns-release-annotation-stays-tracked",
        _annotated("coredns/coredns", HOMELAB_TOOLS),
        "minor",
        "enabled",
        None,
    ),
    *(
        Row(
            f"python-{d.file}-{u}-group",
            d,
            u,
            "groupName",
            Has("python (manual"),
            shares="python",
        )
        for d in PYTHON_SITES
        for u in ("minor", "major")
    ),
    Row(
        "python-rule-without-a-site-drops-it",
        PYTHON_PYENV,
        "minor",
        "groupName",
        None,
        rules=edited(PYTHON_RULE, _drop_file(".python-version")),
    ),
    Row(
        "python-ci-patch-stays-out-of-the-group",
        PYTHON_CI,
        "patch",
        "groupName",
        "{{depName}}",
    ),
    Row(
        "python-k8s-digest-stays-out-of-the-group",
        PYTHON_K8S[1],
        "digest",
        "groupName",
        "k8s image {{depName}}",
    ),
    *(
        Row(f"python-{d.file}-minor-automerge", d, "minor", "automerge", False)
        for d in PYTHON_K8S
    ),
]
