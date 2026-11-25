"""The k8s family of `render_templates.py`: every file the renderer writes for a k8s tag.

A Jinja indentation bug is exactly the class ``check-yaml`` misses — it does not render
``.j2`` — so it passes CI and first appears as ``error validating data`` partway through an
apply, with some objects already applied and some not.

The files are ``render_tag``'s in stub mode, so what this checks is what Flux applies, with a
stub in place of each secret. Structural check only: no SOPS access is needed.

Every parsed object is then validated against the Kubernetes OpenAPI schema for
``K8S_SCHEMA_VERSION``, vendored from Kubernetes itself and made strict, so an undefined field is
an error. That is the check ``--dry-run`` makes against the live API server, made offline
instead: no cluster, and on a PR. A CRD is checked against its vendored catalog schema, and one
with neither is reported as skipped rather than passed.

Also cross-checks every ``persistentVolumeClaim.claimName`` against the PVC names actually
rendered across the whole tree (a Deployment mounting a PVC nothing declares passes admission —
PVC binding is a scheduling concern, not a validating webhook — so this is otherwise only caught
live). Reported as ``[WARN]``, not folded into the failure count. Promote to a hard failure (fold
`unresolved` into `failures` in run()) once it has run clean — no false positive — for a while.

The rendering and parsing pieces live under ``scripts/lib/`` — ``k8s_roles`` (which roles are
rendered and which are exempt), ``k8s_context`` (Ansible's variable semantics), ``k8s_yaml``
(the strict loaders, the ``lookup()`` stub and the claim names). The two rule modules
only this family imports sit in ``validate_lib/``: ``k8s_schema`` (the OpenAPI and
vendored-CRD checks) and ``k8s_net_rules`` (the two semantic rules no schema can make). The
filter registration every render guard shares is ``lib.guard_jinja_env``. This module keeps
``check_template``, the per-template render other guards call, and ``run()``.
"""

import sys
from collections.abc import Callable
from pathlib import Path

from lib.guard_jinja_env import template_env
from lib.k8s_roles import (
    SKIP_ROLES,
    is_manifest_template,
    k8s_entries,
    misplaced_template_lookups,
    non_manifest_documents,
    role_dirs,
    shared_default_templates,
)
from lib.k8s_yaml import (
    find_claim_name_refs,
    find_pvc_names,
    make_lookup,
    parse_docs,
    yaml_error,
)
from lib.render_guard import dump_numbered, render_or_error
from lib.repo_paths import K8S_ROLES, K8S_SERVICES
from validate.validate_lib.k8s_net_rules import (
    https_route_without_tls,
    netpol_port_mismatches,
    service_port_translations,
    workload_container_ports,
)
from validate.validate_lib.k8s_schema import K8S_SCHEMA_VERSION, NO_SCHEMA, schema_error


def check_template(role: str, tpl: Path, ctx: dict) -> tuple[str | None, list]:
    """Render one manifest template.

    Returns (error, docs) — docs is [] whenever error is set, otherwise the manifest's parsed YAML
    documents (for the PVC claimName cross-reference check in run(), which needs the actual objects
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


def run(ok: Callable[[str], None], render: Callable | None = None) -> tuple[int, int]:
    """Validate what the renderer writes for every k8s role against the k3s schemas.

    Reads each tag's stub-mode `render_tag` output, the bytes Flux would apply with a stub for
    each secret. Checks each file is valid, duplicate-key-free YAML, schema-checks each object
    against `K8S_SCHEMA_VERSION` (or a vendored CRD schema), and cross-references every declared
    PersistentVolumeClaim against every `claimName` reference across all roles.

    Args:
        ok: Prints the `[ok]` line of each clean file.
        render: Takes a tag and its stubs and returns its `RenderedTag`. The default is
            `render_tag`; a test passes one that writes a file the validator must reject.

    Returns:
        (files checked, failures). A failure is a file that did not render, parse or
        schema-check clean.
    """
    # Function-level: the modules that import this one for `check_template` render no tag.
    from renderer.renderer_lib.cluster import RenderError, render_tag
    from renderer.shadow_diff import stub_vars

    entries = k8s_entries()
    stubs = stub_vars()
    render = render or render_tag

    roles = role_names(K8S_ROLES)
    checked = failures = 0
    # The claimName cross-reference runs once after the loop: a reference in role A can name a PVC
    # role B declares.
    pvc_names: set[str] = set()
    parsed_templates: list[tuple[str, list]] = []
    for role in roles:
        # Not every .j2 in templates/ is a manifest (helper scripts, Dockerfiles); the glob is
        # non-recursive so app configs in templates/config/ are not parsed as manifests.
        templates = sorted(
            p
            for p in (K8S_ROLES / role / "templates").glob("*.j2")
            if is_manifest_template(p)
        )
        if not templates:
            # A role that only delegates to image-builder owns no manifests; it still needs an
            # inventory entry, so the check below is not skipped.
            if role in entries:
                continue
            print(f"  [FAIL] {role}: no manifest templates found", file=sys.stderr)
            failures += 1
            continue
        # A k8s role with no inventory entry would render every port and hostname as STUB and
        # quietly pass.
        if role not in entries:
            print(
                f"  [FAIL] {role}: no entry in {K8S_SERVICES.name}",
                file=sys.stderr,
            )
            failures += 1
            continue

        # Placement, read off the source: an embedded app config at templates/ top level would be
        # parsed as a manifest and checked as nothing, since the schema pass skips a document with
        # no `kind`.
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
                # A top-level template rendering to a document with no `kind` is not a manifest, and
                # nothing downstream would look at it.
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

    # WARNING ONLY, deliberately: a false positive must not block a deploy. This proves only that
    # the referenced NAME exists in what got rendered; PVC binding is a scheduling concern.
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

    # IngressRoute is a CRD, so `schema_error` skips it, and a NetworkPolicy port passes any schema
    # whatever number it holds; both apply cleanly and then match nothing.
    for rel, docs in parsed_templates:
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            err = https_route_without_tls(doc)
            if err:
                failures += 1
                print(f"  [FAIL] {rel}: {err}", file=sys.stderr)

    # WARNING ONLY, as the claimName check: a false positive on the network plane must not block a
    # deploy.
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

    # A hard failure: an error here is what `kubectl apply --dry-run=server` would reject, leaving
    # an apply half done.
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

    # Printed rather than silent: an unschema'd kind is unvalidated. This should read 0; a CRD kind
    # here means no vendored schema matched it, which tests/k8s/test_rendered_properties.py fails
    # on.
    if skipped_kinds:
        total = sum(skipped_kinds.values())
        detail = ", ".join(f"{k} x{v}" for k, v in sorted(skipped_kinds.items()))
        print(
            f"\n{total} object(s) matched neither the v{K8S_SCHEMA_VERSION} core schema nor a "
            f"vendored CRD schema, and were NOT schema-checked: {detail}\n"
            "Vendor one with scripts/validate/refresh_vendored_schemas.py."
        )

    return checked, failures
