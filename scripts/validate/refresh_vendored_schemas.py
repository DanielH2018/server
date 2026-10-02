#!/usr/bin/env python3
"""Re-download the vendored JSON schemas this repo checks rendered config against.

Three sets, refreshed together because they age the same way and are reviewed the same way:
Kubernetes' own OpenAPI v3 schemas for the core kinds ``validate/k8s_manifests.py`` checks
manifests against, the CRD schemas it checks custom resources against, and Authelia's own
configuration schema that ``ansible/tests/services/test_authelia_config_schema.py`` checks the
rendered portal config against.

THE CORE SCHEMAS COME FROM KUBERNETES ITSELF. They are the per-group files under
``api/openapi-spec/v3/`` in the kubernetes/kubernetes repo, at the tag ``k3s_version`` names, so
the check validates against the API the cluster actually runs, and a new minor needs nothing but
this script once k3s ships it. Until #2367 they came from the kubernetes-validate package, which
went quiet after publishing 1.36 and so held the k3s upgrade behind it. Each file is cut to its
``components.schemas`` with every ``description`` dropped, which takes the seven files from
5.3 MB to about 310 KB, and ``SOURCE.json`` beside them records the tag and the sha256 of each
file as upstream served it.

WHY THESE ARE VENDORED RATHER THAN FETCHED. `validate/k8s_manifests.py` runs as a prek hook, on
every commit that touches a manifest template. A hook that resolves DNS is a hook that fails
when DNS is down — and this repo *is* the DNS: Pi-hole, unbound and the host resolver all live
here, and a session fixing a broken resolver must still be able to commit. So the schemas are a
snapshot on disk, and the network cost is paid deliberately by running this script.

WHAT AGES, AND WHAT CATCHES IT. A vendored schema drifts from the CRD the cluster actually
serves, with no signal — the failure mode this repo has a memory entry for. Two things bound it.
`test_every_rendered_kind_has_a_vendored_schema` fails the moment a manifest renders a kind, core
or CRD, with no schema here, so a NEW kind cannot arrive unvalidated. And this script is
idempotent: run it, and a non-empty `git diff` under schemas/ is the drift.

Refresh: uv run python scripts/validate/refresh_vendored_schemas.py
"""

import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# `lib` is a sibling directory under `scripts/`; a directly-invoked script gets only its
# own directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli_help import answer_help

# datreeio/CRDs-catalog publishes one JSON Schema per CRD kind, laid out by API group. It is the
# schema source kubeconform's own docs point at, so the layout is a de-facto convention rather
# than one project's choice.
CATALOG = "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main"

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
K3S_DEFAULTS = (
    Path(__file__).resolve().parents[2]
    / "ansible"
    / "roles"
    / "setup"
    / "k3s"
    / "defaults"
    / "main.yml"
)
KUBERNETES_OPENAPI = "https://raw.githubusercontent.com/kubernetes/kubernetes/{tag}/api/openapi-spec/v3/{name}"

# The apiVersions this repo renders, as upstream names their files. Kept in step with the tree by
# test_every_rendered_kind_has_a_vendored_schema, which reads the manifests, so a manifest in a
# new group fails there until its group is added here.
CORE_GROUP_FILES = [
    "api__v1_openapi.json",
    "apis__apps__v1_openapi.json",
    "apis__batch__v1_openapi.json",
    "apis__discovery.k8s.io__v1_openapi.json",
    "apis__networking.k8s.io__v1_openapi.json",
    "apis__rbac.authorization.k8s.io__v1_openapi.json",
    "apis__storage.k8s.io__v1_openapi.json",
]

# (group, kind, version) for every CRD this repo renders. Kept in step with the tree by
# test_every_rendered_kind_has_a_vendored_schema, which reads the manifests rather than
# this list — so adding a kind here without a manifest is harmless, and adding a manifest
# without a kind here fails that test.
VENDORED = [
    ("traefik.io", "ingressroute", "v1alpha1"),
    ("traefik.io", "middleware", "v1alpha1"),
    ("traefik.io", "tlsoption", "v1alpha1"),
]

# Authelia publishes a JSON Schema per MINOR release of its own configuration file, and it sets
# `additionalProperties: false` throughout — which is the property the check needs, because
# Authelia refuses to start on a key it does not recognise and nothing else between the editor
# and the SSO gate can see one.
#
# Pinned to the minor rather than tracking `latest`: `authelia_k8s_image` pins a patch release,
# and a schema from a later minor would accept keys the running binary rejects — the exact
# failure the check exists to catch, inverted.
# `test_the_vendored_schema_matches_the_pinned_image` fails when the image pin and this move
# apart, so a Renovate minor bump lands here rather than in a crashloop.
AUTHELIA_SCHEMA_MINOR = "v4.39"
AUTHELIA_SCHEMA_URL = f"https://www.authelia.com/schemas/{AUTHELIA_SCHEMA_MINOR}/json-schema/configuration.json"
AUTHELIA_SCHEMA_PATH = ("authelia.com", f"configuration_{AUTHELIA_SCHEMA_MINOR}.json")


def kubernetes_tag(k3s_defaults: str) -> str:
    """The kubernetes/kubernetes tag a k3s release is built from: v1.36.4+k3s1 -> v1.36.4."""
    match = re.search(
        r"^k3s_version:\s*(v\d+\.\d+\.\d+)\+k3s\d+\s*$", k3s_defaults, re.MULTILINE
    )
    if not match:
        raise ValueError("k3s_version not found in roles/setup/k3s/defaults/main.yml")
    return match.group(1)


def without_descriptions(node, in_properties: bool = False):
    """``node`` with every ``description`` keyword removed. A field NAMED description stays."""
    if isinstance(node, list):
        return [without_descriptions(v) for v in node]
    if not isinstance(node, dict):
        return node
    return {
        key: without_descriptions(
            value, in_properties=key == "properties" and not in_properties
        )
        for key, value in node.items()
        if in_properties or key != "description"
    }


def unresolved_refs(components: dict) -> list[str]:
    """Every ``$ref`` in ``components`` that does not name a component in the same file."""
    missing = []

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.rsplit("/", 1)[-1] not in components:
                missing.append(ref)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(components)
    return missing


def vendor_core_file(body: bytes) -> bytes:
    """The vendored form of one upstream group file: its schemas only, descriptions dropped.

    Compact and in upstream's key order, so the output is a pure function of the input and a
    refresh against an unchanged tag writes identical bytes.
    """
    schemas = json.loads(body)["components"]["schemas"]
    missing = unresolved_refs(schemas)
    if missing:
        raise ValueError(f"$ref outside the file: {sorted(set(missing))[:5]}")
    vendored = {"components": {"schemas": without_descriptions(schemas)}}
    return (json.dumps(vendored, separators=(",", ":")) + "\n").encode()


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def refresh_core() -> int:
    """Vendor Kubernetes' own schemas for the k3s release this repo pins. Returns failures."""
    tag = kubernetes_tag(K3S_DEFAULTS.read_text())
    minor = ".".join(tag.split(".")[:2])
    dest_dir = SCHEMA_DIR / "kubernetes.io" / minor
    dest_dir.mkdir(parents=True, exist_ok=True)
    sources = {}
    failures = 0
    for name in CORE_GROUP_FILES:
        try:
            body = fetch(KUBERNETES_OPENAPI.format(tag=tag, name=name))
            vendored = vendor_core_file(body)
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
            KeyError,
        ) as exc:
            print(f"  [FAIL] kubernetes.io/{minor}/{name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        (dest_dir / name).write_bytes(vendored)
        sources[name] = hashlib.sha256(body).hexdigest()
        print(
            f"  [ok]   kubernetes.io/{minor}/{name} ({len(body)} -> {len(vendored)} bytes)"
        )
    if not failures:
        source = {"tag": tag, "upstream": KUBERNETES_OPENAPI, "sha256": sources}
        (dest_dir / "SOURCE.json").write_text(json.dumps(source, indent=2) + "\n")
    return failures


def main() -> int:
    """Download every vendored schema and write it under `schemas/`.

    The CRD schemas in `VENDORED` come from the CRDs-catalog; Authelia's configuration schema
    comes from its own site at the minor `AUTHELIA_SCHEMA_MINOR` pins.

    Returns:
        0 if every schema downloaded, 1 if any failed.
    """
    # Before any download: `--help` ran a full refresh until this line, so the entry-point
    # help test rewrote the vendored schemas on every run and failed on any refused fetch.
    answer_help(__doc__)
    SCHEMA_DIR.mkdir(exist_ok=True)
    failures = refresh_core()
    targets = [
        (f"{group}/{kind}_{version}.json", f"{CATALOG}/{group}/{kind}_{version}.json")
        for group, kind, version in VENDORED
    ]
    targets.append(("/".join(AUTHELIA_SCHEMA_PATH), AUTHELIA_SCHEMA_URL))
    for name, url in targets:
        dest = SCHEMA_DIR / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            body = fetch(url)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  [FAIL] {name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        # Written as bytes, unparsed: the validator is what must be able to load it, and a
        # reformat here would make every refresh a diff even when nothing upstream changed.
        dest.write_bytes(body)
        print(f"  [ok]   {name} ({len(body)} bytes)")

    total = len(targets) + len(CORE_GROUP_FILES)
    print(
        f"\n{total - failures}/{total} schema(s) refreshed into "
        f"{SCHEMA_DIR.relative_to(Path.cwd()) if SCHEMA_DIR.is_relative_to(Path.cwd()) else SCHEMA_DIR}."
        "\nA non-empty `git diff` under that directory is upstream drift — review it as you "
        "would any dependency bump."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
