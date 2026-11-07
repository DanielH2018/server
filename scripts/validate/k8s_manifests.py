#!/usr/bin/env python3
"""Validate every file the renderer writes for a k8s tag, and assert each parses as valid YAML.

The compose templates have the same guard (``validate/compose_templates.py``), and the k8s
manifests need it for the same reason. A Jinja indentation bug is exactly the class
``check-yaml`` and ``ansible-lint`` miss — neither renders ``.j2`` — so it passes CI and first
appears as ``error validating data`` partway through an apply, with some objects already applied
and some not.

The files are ``render_tag``'s in stub mode, so what this checks is what Flux applies, with a
stub in place of each secret.

Every parsed object is then validated against the Kubernetes OpenAPI schema for
``K8S_SCHEMA_VERSION``, vendored from Kubernetes itself and made strict, so an undefined field is
an error. That is the check ``--dry-run`` makes against the live API server, made offline
instead: no cluster, and on a PR. A CRD is checked against its vendored catalog schema, and one
with neither is reported as skipped rather than passed.

Structural check only: secrets are stubbed, so no SOPS access is needed. Run
directly or via the ``validate-k8s-manifests`` prek hook. Exits non-zero on any render failure
or invalid YAML.

Also cross-checks every ``persistentVolumeClaim.claimName`` against the PVC names actually
rendered across the whole tree (a Deployment mounting a PVC nothing declares passes admission —
PVC binding is a scheduling concern, not a validating webhook — so this is otherwise only caught
live). Reported as ``[WARN]``, not folded into the exit code. Promote to a hard failure (fold
`unresolved` into `failures` in main()) once it has run clean — no false positive — for a while.

The rendering and parsing pieces live under ``scripts/lib/`` — ``k8s_roles`` (which roles are
rendered and which are exempt), ``k8s_context`` (Ansible's variable semantics), ``k8s_yaml``
(the strict loaders, the ``lookup()`` stub and the claim names). The two rule modules
only this validator imports sit in ``validate_lib/``: ``k8s_schema`` (the OpenAPI and
vendored-CRD checks) and ``k8s_net_rules`` (the two semantic rules no schema can make). The
filter registration every render guard shares is ``lib.ansible_jinja_env``. This module keeps
``check_template``, the per-template render other guards call, and ``main()``, and re-exports
every moved name so an existing importer keeps working.
"""

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from validate.validate_lib.ok_lines import ok_printer
from lib.cli_help import answer_help
from lib.ansible_inventory import K8S_HOST_VARS
from lib.k8s_context import resolve_vars
from lib.render_context import render_context
from validate.validate_lib.k8s_net_rules import (
    HTTPS_ENTRYPOINT,
    https_route_without_tls,
    netpol_port_mismatches,
    service_port_translations,
    workload_container_ports,
)
from lib.k8s_roles import (
    CALLER_RENDERED_ROLES,
    K8S_ROLES,
    NO_MANIFEST_ROLES,
    SHARED_MANIFEST_DEFAULTS,
    SKIP_ROLES,
    is_manifest_template,
    k8s_entries,
    misplaced_template_lookups,
    non_manifest_documents,
    role_dirs,
    shared_default_templates,
)
from validate.validate_lib.k8s_schema import (
    K8S_SCHEMA_VERSION,
    NO_SCHEMA,
    crd_schema_error,
    crd_schema_path,
    normalise_octal,
    schema_error,
)
from lib.ansible_jinja_env import (
    register_ansible_filters,
    template_env,
)
from lib.k8s_yaml import (
    find_claim_name_refs,
    find_pvc_names,
    make_lookup,
    parse_docs,
    yaml_error,
)
from lib.render_guard import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    SHARED_TPL,
    dump_numbered,
    load_yaml,
    make_env,
    render_or_error,
)

# Re-exported for the ~20 modules that import these from here: the render helpers and inventory
# anchors from `lib.render_guard`, and everything the six `lib.k8s_*` modules were split into.
__all__ = [
    "ALL_VARS",
    "ANSIBLE",
    "BASE_CONTEXT",
    "CALLER_RENDERED_ROLES",
    "HTTPS_ENTRYPOINT",
    "K8S_HOST_VARS",
    "K8S_ROLES",
    "K8S_SCHEMA_VERSION",
    "NO_MANIFEST_ROLES",
    "NO_SCHEMA",
    "SHARED_MANIFEST_DEFAULTS",
    "SHARED_TPL",
    "SKIP_ROLES",
    "check_template",
    "crd_schema_error",
    "crd_schema_path",
    "dump_numbered",
    "find_claim_name_refs",
    "find_pvc_names",
    "https_route_without_tls",
    "is_manifest_template",
    "k8s_entries",
    "load_yaml",
    "misplaced_template_lookups",
    "non_manifest_documents",
    "main",
    "make_env",
    "make_lookup",
    "netpol_port_mismatches",
    "normalise_octal",
    "parse_docs",
    "register_ansible_filters",
    "render_context",
    "render_or_error",
    "resolve_vars",
    "schema_error",
    "shared_default_templates",
    "service_port_translations",
    "workload_container_ports",
    "yaml_error",
]


def check_template(role: str, tpl: Path, ctx: dict) -> tuple[str | None, list]:
    """Render one manifest template.

    Returns (error, docs) — docs is [] whenever error is set, otherwise the manifest's parsed YAML
    documents (for the PVC claimName cross-reference check in main(), which needs the actual objects
    rather than just a pass/fail).
    """
    env = template_env(K8S_ROLES / role / "templates")
    env.globals["lookup"] = make_lookup(ctx)
    rendered, err = render_or_error(env, tpl.name, ctx)
    if rendered is None:
        return err, []

    err = yaml_error(rendered)
    if err:
        print(f"\n----- rendered {role}/{tpl.name} -----", file=sys.stderr)
        dump_numbered(rendered)
        return err, []

    return None, parse_docs(rendered)


def role_names(k8s_roles: Path) -> list[str]:
    """Every role under `k8s_roles` this validator renders, sorted: all but `SKIP_ROLES`."""
    return [d.name for d in role_dirs(k8s_roles, exclude=SKIP_ROLES)]


def _manifests(rendered) -> list[tuple[str, str]]:
    """Each file a `RenderedTag` holds, by name, less the `kustomization.yaml` of each directory."""
    from renderer.renderer_lib.bundle import KUSTOMIZATION

    return [
        (name, text)
        for files in (
            rendered.files,
            rendered.deferred,
            rendered.verify,
            rendered.precheck,
        )
        for name, text in files.items()
        if name != KUSTOMIZATION
    ]


def main(argv: Sequence[str] = (), render: Callable | None = None) -> int:
    """Validate what the renderer writes for every k8s role against the k3s schemas.

    Reads each tag's stub-mode `render_tag` output, the bytes Flux would apply with a stub for
    each secret. Checks each file is valid, duplicate-key-free YAML, schema-checks each object
    against `K8S_SCHEMA_VERSION` (or a vendored CRD schema), and cross-references every declared
    PersistentVolumeClaim against every `claimName` reference across all roles.

    Args:
        argv: The command-line arguments. `-v` or `--verbose` also lists each clean
            template as an `[ok]` line; by default only failures and the summary print.
        render: Takes a tag and its stubs and returns its `RenderedTag`. The default is
            `render_tag`; a test passes one that writes a file the validator must reject.

    Returns:
        0 if every template rendered, parsed and schema-checked clean; 1 otherwise.
    """
    answer_help(__doc__, list(argv))
    # Function-level: the ~20 modules that import this one for its re-exports render nothing.
    from renderer.renderer_lib.cluster import RenderError, render_tag
    from renderer.shadow_diff import stub_vars

    ok = ok_printer(argv)
    entries = k8s_entries()
    stubs = stub_vars()
    render = render or render_tag

    roles = role_names(K8S_ROLES)
    checked = failures = 0
    # Built up alongside the existing per-template loop below (one render pass, not two): every
    # declared PVC name (from a rendered PersistentVolumeClaim), and every (rel, docs) this run successfully parsed. The
    # claimName cross-reference check runs once, after the loop, once the PVC index is complete
    # — a reference in role A can legitimately name a PVC role B declares (media_volume_claim,
    # ~7 consumers), so it can't be checked role-by-role as the loop goes.
    pvc_names: set[str] = set()
    parsed_templates: list[tuple[str, list]] = []
    for role in roles:
        # Not every .j2 in a k8s role's templates/ is a manifest — a role may also ship a
        # helper script (registry's registry-gc.sh.j2) or a Dockerfile for
        # image-builder (homelab-mcp). Shell is rendered and linted by
        # validate/shell_templates.py; a Dockerfile is consumed by buildctl. Parsing
        # either here reports a comment line as malformed YAML.
        #
        # The glob is deliberately non-recursive, which is what makes templates/config/
        # work: an app config a manifest embeds via lookup() (CouchDB's local.ini,
        # Home Assistant's configuration.yaml) is usually not YAML at all and must not be
        # parsed as a manifest. Those live one level down, in templates/config/, and are
        # validated by whatever tool understands their format, not by this script.
        templates = sorted(
            p
            for p in (K8S_ROLES / role / "templates").glob("*.j2")
            if is_manifest_template(p)
        )
        if not templates:
            # A role that only delegates — it calls image-builder and owns no manifests of its
            # own. Not a failure, but it must still have an inventory entry, so the check below
            # is deliberately not skipped with it.
            if role in entries:
                continue
            print(f"  [FAIL] {role}: no manifest templates found", file=sys.stderr)
            failures += 1
            continue
        # A k8s role with no inventory entry would render every port and hostname as STUB and
        # quietly pass, so treat the mismatch as the failure it is.
        if role not in entries:
            print(
                f"  [FAIL] {role}: no platform: k8s entry in {K8S_HOST_VARS.name}",
                file=sys.stderr,
            )
            failures += 1
            continue

        # Placement, read off the source: an embedded app config that sits at templates/ top
        # level is parsed as a manifest below and checked as nothing, since the schema pass
        # skips a document with no `kind`.
        for tpl in [*templates, *shared_default_templates(role)]:
            for target in misplaced_template_lookups(tpl.read_text()):
                failures += 1
                print(
                    f"  [FAIL] {role}/{tpl.name}: lookup('template') embeds templates/{target}, "
                    "which is app config at templates/ top level — move it to templates/config/",
                    file=sys.stderr,
                )
        try:
            rendered = render(role, stubs)
        except RenderError as exc:
            failures += 1
            print(f"  [FAIL] {role}: {exc}", file=sys.stderr)
            continue
        for name, text in _manifests(rendered):
            checked += 1
            rel = f"{role}/{name}"
            err = yaml_error(text)
            if err:
                print(f"\n----- rendered {rel} -----", file=sys.stderr)
                dump_numbered(text)
                failures += 1
                print(f"  [FAIL] {rel}: {err}", file=sys.stderr)
                continue
            docs = parse_docs(text)
            kindless = non_manifest_documents(docs)
            if kindless:
                # The other half of the same placement rule, for a config nothing embeds by
                # a literal path: a top-level template that renders to a document with no
                # `kind` is not a manifest, and nothing downstream would look at it.
                failures += 1
                print(
                    f"  [FAIL] {rel}: renders {len(kindless)} document(s) with no `kind` — "
                    "not a Kubernetes manifest; app config belongs in templates/config/",
                    file=sys.stderr,
                )
                continue
            for doc in docs:
                pvc_names.update(find_pvc_names(doc))
            parsed_templates.append((rel, docs))
            ok(f"  [ok]   {rel}")

    # WARNING ONLY, deliberately — not folded into `failures`. This is new and unproven against
    # the real tree; a false positive here must not be able to block a deploy the way a `[FAIL]`
    # does. Promote to a hard failure once it's run clean for a while (see the module docstring
    # note below this function). PVC binding itself is still a scheduling concern this can't see
    # — this only proves the referenced NAME exists somewhere in what got rendered.
    unresolved = 0
    for rel, docs in parsed_templates:
        for doc in docs:
            for claim in find_claim_name_refs(doc):
                if claim not in pvc_names:
                    unresolved += 1
                    print(
                        f"  [WARN] {rel}: claimName {claim!r} matches no rendered "
                        "PersistentVolumeClaim",
                        file=sys.stderr,
                    )
    if unresolved:
        print(
            f"\n{unresolved} claimName reference(s) match no rendered PVC — WARNING ONLY, does "
            "not fail the build. A brand-new service naming a PVC that doesn't exist yet would "
            "show up here.",
            file=sys.stderr,
        )

    # Two semantic rules the schema check structurally cannot make. IngressRoute is a CRD, so
    # its schema lives in the cluster and `schema_error` skips it entirely; a NetworkPolicy's
    # port passes any schema whatever number it holds. Both failures are silent in the same
    # way — the object applies cleanly and then matches nothing.
    for rel, docs in parsed_templates:
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            err = https_route_without_tls(doc)
            if err:
                failures += 1
                print(f"  [FAIL] {rel}: {err}", file=sys.stderr)

    # WARNING ONLY, on the same reasoning as the claimName check above: this is new and
    # unproven against the real tree, and a false positive on the network plane must not be
    # able to block a deploy. Promote to a hard failure once it has run clean for a while.
    all_docs = [
        doc for _, docs in parsed_templates for doc in docs if isinstance(doc, dict)
    ]
    by_labels = workload_container_ports(all_docs)
    translations = service_port_translations(all_docs)
    for rel, docs in parsed_templates:
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            for problem in netpol_port_mismatches(doc, by_labels, translations):
                print(f"  [WARN] {rel}: {problem}", file=sys.stderr)

    # Schema validation, over the objects the loop already parsed. A hard failure, not a
    # warning: an error here is exactly what `kubectl apply --dry-run=server` would reject, so
    # letting it through would put a manifest on the cluster that the API server refuses
    # partway through an apply — some objects applied, some not.
    schema_failures = 0
    skipped_kinds: dict[str, int] = {}
    for rel, docs in parsed_templates:
        for doc in docs:
            if not isinstance(doc, dict) or "kind" not in doc:
                continue
            err = schema_error(doc)
            if err is NO_SCHEMA:
                kind = f"{doc.get('apiVersion')}/{doc.get('kind')}"
                skipped_kinds[kind] = skipped_kinds.get(kind, 0) + 1
            elif err:
                schema_failures += 1
                print(
                    f"  [FAIL] {rel}: {doc.get('kind')} fails the "
                    f"v{K8S_SCHEMA_VERSION} schema — {err}",
                    file=sys.stderr,
                )
    failures += schema_failures

    # Printed rather than silent: an unschema'd kind is unvalidated, and the only honest way to
    # report coverage is to name what was not covered. A CRD count that jumps means a new
    # custom resource arrived with nothing checking its shape. This should read 0 — a CRD kind
    # reaching here means no vendored schema matched it, which the
    # `rendered-kinds-have-vendored-schemas` row of ansible/tests/k8s/test_rendered_properties.py
    # fails on.
    if skipped_kinds:
        total = sum(skipped_kinds.values())
        detail = ", ".join(f"{k} x{v}" for k, v in sorted(skipped_kinds.items()))
        print(
            f"\n{total} object(s) matched neither the v{K8S_SCHEMA_VERSION} core schema nor a "
            f"vendored CRD schema, and were NOT schema-checked: {detail}\n"
            "Vendor one with scripts/validate/refresh_vendored_schemas.py."
        )

    print(f"\n{checked} k8s manifest template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
