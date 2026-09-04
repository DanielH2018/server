#!/usr/bin/env python3
"""Render every k8s manifest template with stubbed vars and assert each parses as valid YAML.

The Docker side has had this guard since the compose templates existed
(``validate/compose_templates.py``); the k8s manifests introduced in slice 1 of the k3s
migration need the same one for the same reason. A Jinja indentation bug is exactly the class
``check-yaml`` and ``ansible-lint`` miss — neither renders ``.j2`` — so it passes CI and first
appears as ``error validating data`` partway through a ``kubectl apply``, with some objects
already applied and some not.

Each role's ``container_item`` comes from daniel-box's real ``containers_list`` rather than a
stub, so the port and hostname a manifest renders with are the ones a deploy would use.

Every parsed object is then validated against the Kubernetes OpenAPI schema for
``K8S_SCHEMA_VERSION`` (``strict=True``, so an undefined field is an error). That is the check
``--dry-run`` makes against the live API server, made offline instead: no cluster, on a PR, and
covering the roles ``k8s_dry_run_unsupported`` refuses. CRDs have no upstream schema and are
reported as skipped rather than passed.

Structural check only: secrets are stubbed (StubUndefined), so no SOPS access is needed. Run
directly or via the ``validate-k8s-manifests`` prek hook. Exits non-zero on any render failure
or invalid YAML.

Also cross-checks every ``persistentVolumeClaim.claimName`` against the PVC names actually
rendered across the whole tree (a Deployment mounting a PVC nothing declares passes admission —
PVC binding is a scheduling concern, not a validating webhook — so this is otherwise only caught
live). Reported as ``[WARN]``, not folded into the exit code: added 2026-08-17, unproven against
the real tree yet. Promote to a hard failure (fold `unresolved` into `failures` in main()) once
it has run clean — no false positive — for a while.

The rendering, parsing and rule pieces live under ``scripts/lib/`` since 2026-09-04 —
``k8s_roles`` (which roles are rendered and which are exempt), ``k8s_context`` (Ansible's
variable semantics), ``k8s_yaml`` (the strict loaders and the ``lookup()`` stub), ``k8s_pvc``
(claim names), ``k8s_schema`` (the OpenAPI and vendored-CRD checks) and ``k8s_net_rules`` (the
two semantic rules no schema can make). This module keeps the Ansible filter registration, the
per-template render and ``main()``, and re-exports every moved name so an existing importer
keeps working.
"""

import hashlib
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.k8s_context import (
    ansible_bool,
    colliding_default_keys,
    resolve_vars,
    role_defaults,
)
from lib.k8s_net_rules import (
    HTTPS_ENTRYPOINT,
    https_route_without_tls,
    netpol_port_mismatches,
    service_port_translations,
    workload_container_ports,
)
from lib.k8s_pvc import (
    find_claim_name_refs,
    find_pvc_names,
    parse_docs,
    volume_claim_pvc_names,
)
from lib.k8s_roles import (
    CALLER_RENDERED_ROLES,
    HOST_VARS,
    K8S_ROLES,
    NO_MANIFEST_ROLES,
    SKIP_ROLES,
    is_manifest_template,
    k8s_entries,
)
from lib.k8s_schema import (
    K8S_SCHEMA_VERSION,
    NO_SCHEMA,
    crd_schema_error,
    crd_schema_path,
    normalise_octal,
    schema_error,
)
from lib.k8s_yaml import make_lookup, yaml_error
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

sys.path.insert(0, str(ANSIBLE / "filter_plugins"))
from toposort import filter_by_platform

from ansible.plugins.filter.core import to_bool

# Re-exported for the ~20 modules that import these from here: the render helpers and inventory
# anchors from `lib.render_guard`, and everything the six `lib.k8s_*` modules were split into.
__all__ = [
    "ALL_VARS",
    "ANSIBLE",
    "BASE_CONTEXT",
    "CALLER_RENDERED_ROLES",
    "HOST_VARS",
    "HTTPS_ENTRYPOINT",
    "K8S_ROLES",
    "K8S_SCHEMA_VERSION",
    "NO_MANIFEST_ROLES",
    "NO_SCHEMA",
    "SHARED_TPL",
    "SKIP_ROLES",
    "ansible_bool",
    "check_template",
    "colliding_default_keys",
    "crd_schema_error",
    "crd_schema_path",
    "dump_numbered",
    "find_claim_name_refs",
    "find_pvc_names",
    "https_route_without_tls",
    "is_manifest_template",
    "k8s_entries",
    "load_yaml",
    "main",
    "make_env",
    "make_lookup",
    "netpol_port_mismatches",
    "normalise_octal",
    "parse_docs",
    "register_ansible_filters",
    "render_or_error",
    "resolve_vars",
    "role_defaults",
    "schema_error",
    "service_port_translations",
    "volume_claim_pvc_names",
    "workload_container_ports",
    "yaml_error",
]


def _ansible_hash(value, algo="sha1"):
    """Mirror Ansible's `hash` filter so templates using it render identically here.

    Same shim as `validate/compose_templates.py`'s `_ansible_hash`, kept as its own copy per
    that module's convention: each render guard owns the Ansible pieces its own templates
    reach for, rather than share a cross-guard import for a five-line function.
    """
    return hashlib.new(algo, str(value).encode("utf-8")).hexdigest()


def register_ansible_filters(env):
    """Register the Ansible filters the manifest templates use on a bare Jinja env.

    `bool` is ansible-core's own `to_bool`, not Python's `bool()`: `bool("false")` is True, so a
    hand-rolled shim would render `{% if x | bool %}` the opposite way from a real deploy and
    report clean on exactly the string/boolean divergence `| bool` is written to prevent.

    pihole's ConfigMap includes the shared dnsmasq template, which derives its override records
    from the inventory via the repo's filter plugin — register the real thing for that too.

    `hash` backs `ansible/templates/checksum-annotation.yml.j2`'s path-mode call
    (`lookup('file', path, rstrip=False) | hash('sha1')`).
    """
    env.filters["bool"] = to_bool
    env.filters["filter_by_platform"] = filter_by_platform
    env.filters["hash"] = _ansible_hash
    return env


def check_template(role: str, tpl: Path, ctx: dict) -> tuple[str | None, list]:
    """Render one manifest template.

    Returns (error, docs) — docs is [] whenever error is set, otherwise the manifest's parsed YAML
    documents (for the PVC claimName cross-reference check in main(), which needs the actual objects
    rather than just a pass/fail).
    """
    env = make_env([K8S_ROLES / role / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    register_ansible_filters(env)
    rendered, err = render_or_error(env, tpl.name, ctx)
    if rendered is None:
        return err, []

    err = yaml_error(rendered)
    if err:
        print(f"\n----- rendered {role}/{tpl.name} -----", file=sys.stderr)
        dump_numbered(rendered)
        return err, []

    return None, parse_docs(rendered)


def main() -> int:
    """Render every k8s role's manifest templates and validate them against the k3s schemas.

    Renders each role's `templates/*.j2` manifests with a real (unstubbed) group_vars/host_vars
    context, checks the result is valid, duplicate-key-free YAML, schema-checks each object
    against `K8S_SCHEMA_VERSION` (or a vendored CRD schema), and cross-references every declared
    PersistentVolumeClaim against every `claimName` reference across all roles.

    Returns:
        0 if every template rendered, parsed and schema-checked clean; 1 otherwise.
    """
    # playbook_dir is real, not stubbed: templates use it to build lookup('file', ...) paths
    # into the Docker roles, and a stubbed value would make those paths unreadable.
    # group_vars values are resolved against each other first — several reference siblings
    # (k8s_registry_pull_host is "localhost:{{ k8s_registry_port }}"), and a role default that
    # reaches one of those needs it already expanded.
    # daniel-box's host_vars are layered over group_vars, which is Ansible's own precedence.
    # containers_list was already read from this file (k8s_entries) so that ports and hostnames
    # render as a deploy would; the rest of the file has to come with it for the same reason.
    # Without it a var defined only there renders as STUB — `render_gid: 993` reached
    # jellyfin's and tdarr's securityContext.supplementalGroups as the string "STUB", which
    # parses as valid YAML and is caught only by the schema check below.
    base = {
        **BASE_CONTEXT,
        **load_yaml(ALL_VARS),
        **load_yaml(HOST_VARS),
        "playbook_dir": str(ANSIBLE),
    }
    base = resolve_vars(base, base)
    entries = k8s_entries()

    roles = sorted(
        d.name for d in K8S_ROLES.iterdir() if d.is_dir() and d.name not in SKIP_ROLES
    )
    checked = failures = 0
    # Built up alongside the existing per-template loop below (one render pass, not two): every
    # declared PVC name (from a rendered PersistentVolumeClaim, or from a volume-claim include —
    # see volume_claim_pvc_names), and every (rel, docs) this run successfully parsed. The
    # claimName cross-reference check runs once, after the loop, once the PVC index is complete
    # — a reference in role A can legitimately name a PVC role B declares (media_volume_claim,
    # ~7 consumers), so it can't be checked role-by-role as the loop goes.
    pvc_names: set[str] = set()
    parsed_templates: list[tuple[str, list]] = []
    # The env volume_claim_pvc_names renders a claim name with. Built once here rather than
    # inside that function so the function needs nothing from this module.
    claim_env = make_env([SHARED_TPL])
    for role in roles:
        # Not every .j2 in a k8s role's templates/ is a manifest — a role may also ship a
        # helper script (claude-otel's telemetry-health.sh.j2) or a Dockerfile for
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
            # A role that only delegates — n8n-images calls image-builder twice and owns no
            # manifests of its own. Not a failure, but it must still have an inventory entry,
            # so the check below is deliberately not skipped with it.
            if role in entries:
                continue
            print(f"  [FAIL] {role}: no manifest templates found", file=sys.stderr)
            failures += 1
            continue
        # A k8s role with no inventory entry would render every port and hostname as STUB and
        # quietly pass, so treat the mismatch as the failure it is.
        if role not in entries:
            print(
                f"  [FAIL] {role}: no platform: k8s entry in {HOST_VARS.name}",
                file=sys.stderr,
            )
            failures += 1
            continue

        role_vars = role_defaults(role, base)
        collisions = colliding_default_keys(role_vars, base)
        if collisions:
            print(
                f"  [FAIL] {role}: defaults/main.yml redefines inventory key(s) "
                f"{sorted(collisions)} — role defaults are Ansible's weakest layer but outrank "
                f"`base` here, so this renders a value a deploy would not. Rename, or drop the "
                f"role default and keep the inventory one.",
                file=sys.stderr,
            )
            failures += 1
            continue

        # DECIDED: role defaults are merged LAST here, so they outrank the inventory — the
        # reverse of Ansible's own precedence, where role defaults are the weakest layer. The
        # inversion is held harmless by `colliding_default_keys` above rather than corrected,
        # because swapping the merge order changes the render context of every role at once to
        # fix a collision that does not exist. Full reasoning in that function's docstring.
        # Contradict it with a case where the guard passes and the render is still wrong.
        ctx = {**base, **role_vars, "container_item": entries[role]}
        pvc_names.update(volume_claim_pvc_names(role, ctx, claim_env))
        for tpl in templates:
            checked += 1
            err, docs = check_template(role, tpl, ctx)
            rel = f"{role}/{tpl.name}"
            if err:
                failures += 1
                print(f"  [FAIL] {rel}: {err}", file=sys.stderr)
            else:
                for doc in docs:
                    pvc_names.update(find_pvc_names(doc))
                parsed_templates.append((rel, docs))
                print(f"  [ok]   {rel}")

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
    # reaching here means no vendored schema matched it, which
    # test_every_rendered_crd_kind_has_a_vendored_schema fails on.
    if skipped_kinds:
        total = sum(skipped_kinds.values())
        detail = ", ".join(f"{k} x{v}" for k, v in sorted(skipped_kinds.items()))
        print(
            f"\n{total} object(s) matched neither the v{K8S_SCHEMA_VERSION} core schema nor a "
            f"vendored CRD schema, and were NOT schema-checked: {detail}\n"
            "Vendor one with scripts/validate/refresh_crd_schemas.py."
        )

    print(f"\n{checked} k8s manifest template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
