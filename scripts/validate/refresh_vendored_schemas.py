#!/usr/bin/env python3
"""Re-download the vendored JSON schemas this repo checks rendered config against.

Two sets, refreshed together because they age the same way and are reviewed the same way: the
CRD schemas ``validate/k8s_manifests.py`` checks manifests against, and Authelia's own
configuration schema that ``ansible/tests/services/test_authelia_config_schema.py`` checks the
rendered portal config against.

WHY THESE ARE VENDORED RATHER THAN FETCHED. `validate/k8s_manifests.py` runs as a prek hook, on
every commit that touches a manifest template. A hook that resolves DNS is a hook that fails
when DNS is down — and this repo *is* the DNS: Pi-hole, unbound and the host resolver all live
here, and a session fixing a broken resolver must still be able to commit. So the schemas are a
snapshot on disk, and the network cost is paid deliberately by running this script.

WHAT AGES, AND WHAT CATCHES IT. A vendored schema drifts from the CRD the cluster actually
serves, with no signal — the failure mode this repo has a memory entry for. Two things bound it.
`test_every_rendered_crd_kind_has_a_vendored_schema` fails the moment a manifest renders a CRD
kind with no schema here, so a NEW kind cannot arrive unvalidated. And this script is
idempotent: run it, and a non-empty `git diff` under schemas/ is the drift.

Refresh: uv run python scripts/validate/refresh_vendored_schemas.py
"""

import sys
import urllib.error
import urllib.request
from pathlib import Path

# datreeio/CRDs-catalog publishes one JSON Schema per CRD kind, laid out by API group. It is the
# schema source kubeconform's own docs point at, so the layout is a de-facto convention rather
# than one project's choice.
CATALOG = "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main"

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

# (group, kind, version) for every CRD this repo renders. Kept in step with the tree by
# test_every_rendered_crd_kind_has_a_vendored_schema, which reads the manifests rather than
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
# and the SSO gate can see one (#1608).
#
# Pinned to the minor rather than tracking `latest`: `authelia_k8s_image` pins a patch release,
# and a schema from a later minor would accept keys the running binary rejects — the exact
# failure the check exists to catch, inverted.
# `test_the_vendored_schema_matches_the_pinned_image` fails when the image pin and this move
# apart, so a Renovate minor bump lands here rather than in a crashloop.
AUTHELIA_SCHEMA_MINOR = "v4.39"
AUTHELIA_SCHEMA_URL = f"https://www.authelia.com/schemas/{AUTHELIA_SCHEMA_MINOR}/json-schema/configuration.json"
AUTHELIA_SCHEMA_PATH = ("authelia.com", f"configuration_{AUTHELIA_SCHEMA_MINOR}.json")


def main() -> int:
    """Download every vendored schema and write it under `schemas/`.

    The CRD schemas in `VENDORED` come from the CRDs-catalog; Authelia's configuration schema
    comes from its own site at the minor `AUTHELIA_SCHEMA_MINOR` pins.

    Returns:
        0 if every schema downloaded, 1 if any failed.
    """
    SCHEMA_DIR.mkdir(exist_ok=True)
    failures = 0
    targets = [
        (f"{group}/{kind}_{version}.json", f"{CATALOG}/{group}/{kind}_{version}.json")
        for group, kind, version in VENDORED
    ]
    targets.append(("/".join(AUTHELIA_SCHEMA_PATH), AUTHELIA_SCHEMA_URL))
    for name, url in targets:
        dest = SCHEMA_DIR / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                body = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  [FAIL] {name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        # Written as bytes, unparsed: the validator is what must be able to load it, and a
        # reformat here would make every refresh a diff even when nothing upstream changed.
        dest.write_bytes(body)
        print(f"  [ok]   {name} ({len(body)} bytes)")

    print(
        f"\n{len(targets) - failures}/{len(targets)} schema(s) refreshed into "
        f"{SCHEMA_DIR.relative_to(Path.cwd()) if SCHEMA_DIR.is_relative_to(Path.cwd()) else SCHEMA_DIR}."
        "\nA non-empty `git diff` under that directory is upstream drift — review it as you "
        "would any dependency bump."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
