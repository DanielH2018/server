#!/usr/bin/env python3
"""Schema validation for a rendered k8s object: the core OpenAPI check and the vendored CRDs.

Split out of ``scripts/validate/k8s_manifests.py`` on 2026-09-04; that module re-exports every
name here, so an existing importer keeps working.

``CRD_SCHEMA_DIR`` is built from ``repo_paths.SCRIPTS`` rather than from this file's own
``__file__``, because the vendored schemas stay beside the validator that reads them
(``scripts/validate/schemas/``) and ``refresh_crd_schemas.py`` writes them there.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import json
import re
from pathlib import Path

import jsonschema
import kubernetes_validate

from lib.repo_paths import SCRIPTS

__all__ = [
    "CRD_SCHEMA_DIR",
    "K8S_SCHEMA_VERSION",
    "NO_SCHEMA",
    "crd_schema_error",
    "crd_schema_path",
    "normalise_octal",
    "schema_error",
]

# Schema version the rendered manifests are validated against. Must track the cluster: a
# manifest is judged by the API server it will actually be applied to, and validating a 1.37
# field against 1.36 schemas reports a perfectly good manifest as invalid (and vice versa —
# a removed field passes). test_schema_version_matches_k3s in
# scripts/validate/tests/test_validate_k8s_manifests.py ties this to k3s_version in
# roles/setup/k3s/defaults/main.yml so a cluster upgrade cannot leave it behind silently.
K8S_SCHEMA_VERSION = "1.36"

_OCTAL_LITERAL = re.compile(r"^0o[0-7]+$")

# Returned by schema_error for a kind the upstream OpenAPI spec does not describe — a CRD.
NO_SCHEMA = object()


def normalise_octal(node):
    """Convert YAML-1.2 octal literals (``0o444``) to the ints kubectl reads them as.

    PyYAML implements YAML **1.1**, where ``0o444`` is not a number and parses as the STRING
    "0o444"; the parser behind ``kubectl`` reads it as 292. So a manifest that is correct live
    arrives here with a string in an integer field, and the schema check would report four
    perfectly good ``defaultMode: 0o444`` volumes as type errors.

    This is not a guess about which parser wins. The live objects were read while writing this:
    ``scrutiny-web``, ``scrutiny-influxdb`` and ``uptime-kuma`` all carry
    ``secret.defaultMode: 292`` — 0444 — from exactly those templates.

    (The comment above mosquitto's ``defaultMode: 288`` claims the opposite, that kubectl reads
    ``0o440`` as a string. The live values disagree with it. Decimal is still the unambiguous
    spelling and mosquitto is fine as it stands, so nothing is changed there — but do not take
    that comment as the reason to avoid octal literals.)
    """
    if isinstance(node, dict):
        return {k: normalise_octal(v) for k, v in node.items()}
    if isinstance(node, list):
        return [normalise_octal(v) for v in node]
    if isinstance(node, str) and _OCTAL_LITERAL.match(node):
        return int(node, 8)
    return node


CRD_SCHEMA_DIR = SCRIPTS / "validate" / "schemas"


def crd_schema_path(doc: dict) -> Path | None:
    """Where a vendored JSON Schema for this object's apiVersion/kind would live, or None.

    Mirrors datreeio/CRDs-catalog's layout — ``<group>/<lowercase kind>_<version>.json`` — so
    refresh_crd_schemas.py can pull straight from it with no per-kind mapping. A core object
    (apiVersion ``v1``, no group) has no slash and returns None; kubernetes_validate owns those.
    """
    api_version = doc.get("apiVersion")
    kind = doc.get("kind")
    if not isinstance(api_version, str) or not isinstance(kind, str):
        return None
    if "/" not in api_version:
        return None
    group, _, version = api_version.partition("/")
    return CRD_SCHEMA_DIR / group / f"{kind.lower()}_{version}.json"


def crd_schema_error(doc: dict) -> str | None | object:
    """Validate one CRD object against its vendored JSON Schema, or NO_SCHEMA if none exists.

    WHAT THIS CATCHES, precisely — it is narrower than it looks and the difference matters.
    The catalog's schemas set ``additionalProperties: false`` on the spec and on each route, so
    a misspelled key is rejected: ``entrypoints`` for ``entryPoints``, ``middleware`` for
    ``middlewares``. They also require ``spec.routes`` and each route's ``match``, and they
    type-check values. That is the same silent class the core check's ``strict=True`` covers —
    the API server ignores an unknown field, so the object applies clean and the setting simply
    never takes effect.

    WHAT IT DOES NOT CATCH: anything semantic. An https IngressRoute with no ``spec.tls`` is a
    valid document and passes here — ``tls`` is optional in the CRD, because plain-HTTP routes
    are legal. That bug class is `https_route_without_tls` below, and it stays the thing that
    catches it. Verified against the vendored schema rather than assumed.
    """
    path = crd_schema_path(doc)
    if path is None or not path.is_file():
        return NO_SCHEMA
    try:
        schema = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return f"vendored schema {path.name} is unreadable: {exc}"
    try:
        jsonschema.validate(normalise_octal(doc), schema)
    except jsonschema.ValidationError as exc:
        where = ".".join(str(p) for p in exc.absolute_path) or "<root>"
        return f"{where}: {exc.message}"
    except jsonschema.SchemaError as exc:
        return f"vendored schema {path.name} is invalid: {exc.message}"
    return None


def schema_error(doc: dict) -> str | None | object:
    """Validate one rendered object against the Kubernetes schema for K8S_SCHEMA_VERSION.

    Returns None when the object validates, NO_SCHEMA when nothing can check its
    apiVersion/kind, and an error string otherwise.

    ``strict=True`` rejects fields the schema does not define, which is the half that catches
    typos: a misspelled ``readinessProb`` is silently ignored by the API server, so the
    Deployment applies clean and the probe simply never runs.

    A CRD has no schema in the upstream OpenAPI spec — it lives in the cluster — so
    kubernetes_validate raises SchemaNotFoundError for every one. Rather than pass, those fall
    through to `crd_schema_error` and the vendored catalog schemas. Before that existed, 60 of
    this tree's objects (46 IngressRoute, 11 Middleware, 3 TLSOption) were counted as skipped
    and checked by nothing.

    This is the check ``--dry-run`` performs against the live API server, done offline and
    without a cluster — so it also covers the roles k8s_dry_run_unsupported refuses.
    """
    try:
        kubernetes_validate.validate(
            normalise_octal(doc), K8S_SCHEMA_VERSION, strict=True
        )
    except kubernetes_validate.SchemaNotFoundError:
        return crd_schema_error(doc)
    except kubernetes_validate.ValidationError as exc:
        path = ".".join(str(p) for p in getattr(exc, "path", []) or [])
        detail = str(exc).split("\n")[0]
        return f"{path or '<root>'}: {detail}" if path else detail
    except kubernetes_validate.InvalidSchemaError as exc:
        return f"schema error: {exc}"
    return None
