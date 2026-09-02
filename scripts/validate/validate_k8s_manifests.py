#!/usr/bin/env python3
"""Render every k8s manifest template with stubbed vars and assert each parses as valid YAML.

The Docker side has had this guard since the compose templates existed
(``validate_compose_templates.py``); the k8s manifests introduced in slice 1 of the k3s
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
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

import jsonschema
import kubernetes_validate
import yaml

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.render_guard import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    SHARED_TPL,
    HOST_VARS as HOST_VARS_DIR,
    dump_numbered,
    load_yaml,
    make_env,
    render_or_error,
)

sys.path.insert(0, str(ANSIBLE / "filter_plugins"))
from toposort import filter_by_platform

from ansible.plugins.filter.core import to_bool


def register_ansible_filters(env):
    """Register the Ansible filters the manifest templates use on a bare Jinja env.

    `bool` is ansible-core's own `to_bool`, not Python's `bool()`: `bool("false")` is True, so a
    hand-rolled shim would render `{% if x | bool %}` the opposite way from a real deploy and
    report clean on exactly the string/boolean divergence `| bool` is written to prevent.

    pihole's ConfigMap includes the shared dnsmasq template, which derives its override records
    from the inventory via the repo's filter plugin — register the real thing for that too.
    """
    env.filters["bool"] = to_bool
    env.filters["filter_by_platform"] = filter_by_platform
    return env


K8S_ROLES = ANSIBLE / "roles" / "k8s"
# The one host that declares k8s services, so this is a single file where the other inventory
# readers walk the whole directory.
HOST_VARS = HOST_VARS_DIR / "daniel-box.yml"
# Helper roles, included by service roles rather than deployed on their own. They have no
# containers_list entry because they are not services, so the platform check below would always
# fail for them.
#
# rollout-drain is pure tasks, waiting on the rollouts a batch of roles queued into
# k8s_pending_rollouts; it lives under roles/k8s/ only so that both deploy.yml and configarr can
# include it by name. cronjob-gate creates a one-off Job from the CALLER's CronJob with `kubectl
# create job --from=cronjob/<name>`, so the pod spec it runs is the caller's rendered manifest.
# volume-snapshot applies one Longhorn Snapshot CR per claim, built inline and piped to `kubectl
# apply -f -` — per-deploy state rather than part of a service's manifest set, and
# `ansible/tests/longhorn/test_volume_snapshot.py` is what checks its shape instead.
#
# Split into the two classes the comment above already distinguishes, because they rot in
# opposite directions and a single set cannot be checked either way. NO_MANIFEST_ROLES makes a
# claim about the tree that `test_skip_roles_classes_hold.py` asserts, so a role that grows its
# first manifest template stops being silently exempt. CALLER_RENDERED_ROLES makes the opposite
# claim, asserted the opposite way, so an entry that stops carrying templates is caught as a
# stale exemption rather than left as a name nobody can justify.
NO_MANIFEST_ROLES = {
    "manifests",
    "rollout-drain",
    "cronjob-gate",
    "volume-snapshot",
    "longhorn-api",  # resolves a fact only, same as cronjob-gate/volume-snapshot
    "volume-revert",  # reverts a volume through kubectl and the Longhorn API
}

# These DO carry manifest templates; they are exempt for a different reason. Their templates
# render only with vars a CALLING role passes on its `include_role` task — which image, which
# Dockerfile, which claim — and this validator reads role defaults and inventory, not task-level
# `vars:` overrides. Rendering them standalone produces STUB-filled manifests that prove nothing.
#
# That exemption is a coverage gap, not a clean bill: four manifests (volume-claim's pvc and
# seed-pod, image-builder's build-job and context-configmap) are parsed as YAML nowhere, and are
# covered only by one securityContext property each in test_seed_pod_security_context.py and
# test_image_builder_security_context.py. Closing it means rendering them against a fixture of
# the caller vars; until then this names the gap where someone will look for it.
CALLER_RENDERED_ROLES = {
    "volume-claim",
    "image-builder",
}

SKIP_ROLES = NO_MANIFEST_ROLES | CALLER_RENDERED_ROLES


def is_manifest_template(path: Path) -> bool:
    """True if this `.j2` under a role's templates/ is a manifest this script should parse.

    A role may also ship a helper script (claude-otel's telemetry-health.sh.j2) or a Dockerfile
    for image-builder (homelab-mcp). Shell is rendered and linted by validate_shell_templates.py;
    a Dockerfile is consumed by buildctl. Parsing either here reports a comment line as malformed
    YAML.

    Module-level so `test_skip_roles_classes_hold.py` asks the same question this script does.
    A test that reimplemented the predicate would keep passing after this one changed.
    """
    return not path.name.endswith(".sh.j2") and not path.name.startswith("Dockerfile")


def k8s_entries() -> dict[str, dict]:
    """containers_list entries for the k8s platform, keyed by service name."""
    entries = load_yaml(HOST_VARS).get("containers_list") or []
    return {c["name"]: c for c in entries if c.get("platform") == "k8s"}


def ansible_bool(value) -> bool:
    """Ansible's `bool` filter: the strings Ansible treats as true, plus ordinary truthiness.

    `-e k8s_dry_run=true` reaches a play as the STRING "true", which is why the filter exists
    at all — and why "false" must map to False here rather than to non-empty-string truthiness.
    """
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "on", "1"}
    return bool(value)


def resolve_vars(values: dict, context: dict, passes: int = 5) -> dict:
    """Expand ``{{ ... }}`` inside variable VALUES, the way Ansible does before templating.

    Ansible resolves a variable's value recursively, so a role default like
    ``n8n_k8s_image: "{{ k8s_registry_pull_host }}/n8n:latest"`` reaches a manifest already
    expanded — and ``k8s_registry_pull_host`` is itself ``"localhost:{{ k8s_registry_port }}"``,
    so one substitution is not enough. Loading the YAML raw expands nothing, and the literal
    braces survive into the rendered manifest, where ``{`` opens a flow mapping and the document
    fails to parse. That surfaces as "invalid YAML" pointing at a perfectly good template, which
    is precisely the diagnosis this guard exists to give correctly.

    Bounded rather than looped-to-fixpoint so a self-referential value fails the render with a
    recursion the operator can see, instead of hanging CI.
    """
    env = make_env([SHARED_TPL])
    # `bool` is an Ansible filter, not a Jinja builtin, so a group_var that uses it renders
    # here as "No filter named 'bool'" — a render failure pointing at a variable that is
    # perfectly valid under Ansible. Shimmed for the same reason the compose guard shims
    # `hash` and the shell guard shims `search`; see make_env's docstring.
    env.filters["bool"] = ansible_bool

    def expand(node, ctx):
        """Ansible templates a variable's value wherever a string sits inside it, not only
        when the whole value IS a string. A list- or dict-valued variable holding `{{ ... }}`
        therefore reaches a template already expanded; scanning only top-level strings left
        the literal braces in place, which is the exact defect this function's docstring
        describes -- one level further down."""
        if isinstance(node, str):
            return env.from_string(node).render(ctx) if "{{" in node else node
        if isinstance(node, list):
            return [expand(n, ctx) for n in node]
        if isinstance(node, dict):
            return {k: expand(v, ctx) for k, v in node.items()}
        return node

    resolved = dict(values)
    for _ in range(passes):
        pending = {k: v for k, v in resolved.items() if "{{" in str(v)}
        if not pending:
            break
        for key, value in pending.items():
            resolved[key] = expand(value, {**context, **resolved})
    return resolved


def role_defaults(role: str, base: dict) -> dict:
    return resolve_vars(load_yaml(K8S_ROLES / role / "defaults" / "main.yml"), base)


def colliding_default_keys(role_vars: dict, base: dict) -> set:
    """The keys a role's defaults and the inventory both define — which must be none.

    The render context below is built `{**base, **role_defaults(...)}`, so a role default
    outranks the group_vars and host_vars merged into `base`. Ansible's own precedence is the
    reverse: role defaults are the WEAKEST layer and host_vars beat them. A shared key therefore
    makes this validator render a value a deploy would never produce, and it passes — the
    manifest is still valid YAML and still schema-checks, just against the wrong number.

    Asserted rather than fixed by swapping the merge order: swapping changes the context of all
    54 roles at once to correct a collision that does not exist today, where failing loudly
    costs nothing until one appears. `crowdsec_k8s_image` was hoisted into all.yml exactly this
    way once, so the hoist that creates one is a real move, not a hypothetical.
    """
    return set(role_vars) & set(base)


def parse_docs(rendered: str) -> list:
    """Parse a rendered manifest into its YAML documents, the same way yaml_error does.

    Only called after yaml_error has already confirmed the render is valid YAML — a raise here would
    be a bug in this function, not in the manifest.
    """
    return list(yaml.load_all(rendered, Loader=_StrictKeyLoader))


def find_pvc_names(doc) -> list[str]:
    """The name of the PVC this document declares, if it is one — a rendered manifest is one
    object per document, so this is a direct check, not a recursive search."""
    if isinstance(doc, dict) and doc.get("kind") == "PersistentVolumeClaim":
        name = (doc.get("metadata") or {}).get("name")
        if isinstance(name, str):
            return [name]
    return []


def find_claim_name_refs(node) -> list[str]:
    """Every `persistentVolumeClaim.claimName` in a parsed manifest, wherever it is nested.

    A Deployment/DaemonSet has it at spec.template.spec.volumes[]; a CronJob one level deeper
    through spec.jobTemplate; a bare Pod at spec.volumes[] directly. Walked generically instead
    of hardcoded per-kind paths, so a shape this wasn't written for (a future StatefulSet, say)
    is still covered rather than silently skipped.
    """
    refs: list[str] = []
    if isinstance(node, dict):
        pvc = node.get("persistentVolumeClaim")
        if isinstance(pvc, dict) and isinstance(pvc.get("claimName"), str):
            refs.append(pvc["claimName"])
        for value in node.values():
            refs.extend(find_claim_name_refs(value))
    elif isinstance(node, list):
        for item in node:
            refs.extend(find_claim_name_refs(item))
    return refs


def volume_claim_pvc_names(role: str, ctx: dict) -> list[str]:
    """PVC names volume-claim creates on this role's behalf.

    volume-claim is in SKIP_ROLES and never rendered under its own role — its templates/pvc.yaml.j2
    (metadata.name: `{{ volume_claim_name }}`) only ever renders with vars a CALLING role passes
    on the `include_role` task (e.g. tdarr's `volume_claim_name: "{{ tdarr_k8s_configs_claim }}"`),
    which is otherwise invisible to this validator — it reads role defaults/templates, not
    task-level `vars:` overrides. Without this, every volume-claim-backed claimName (tdarr,
    freshrss, ...) would show as unresolved. Best-effort: only handles a plain string `vars:`
    value, which is the only form any current caller uses.
    """
    names: list[str] = []
    tasks_dir = K8S_ROLES / role / "tasks"
    if not tasks_dir.is_dir():
        return names
    env = make_env([SHARED_TPL])
    for task_file in sorted(tasks_dir.glob("*.yml")):
        try:
            tasks = yaml.safe_load(task_file.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            inc = task.get("ansible.builtin.include_role")
            if not isinstance(inc, dict) or inc.get("name") != "k8s/volume-claim":
                continue
            claim = (task.get("vars") or {}).get("volume_claim_name")
            if not isinstance(claim, str):
                continue
            try:
                names.append(env.from_string(claim).render(ctx))
            except Exception:  # noqa: S112 -- a claim name this stub context cannot render is not one this validator can check
                continue
    return names


def yaml_error(rendered: str) -> str | None:
    """Return an error string if ``rendered`` is not parseable YAML, else None.

    Also parses YAML *embedded* in ConfigMap/Secret values. The manifest wrapping Traefik's
    static config and Authelia's configuration.yml is trivially valid whatever those blobs
    contain — they are opaque block scalars to the outer document — so checking only the
    outer YAML would miss precisely the indentation bugs that matter most here.
    """
    try:
        docs = list(yaml.load_all(rendered, Loader=_StrictKeyLoader))
    except yaml.YAMLError as exc:
        return f"invalid YAML: {exc}"

    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") not in ("ConfigMap", "Secret"):
            continue
        for field in ("data", "stringData"):
            for key, value in (doc.get(field) or {}).items():
                if not key.endswith((".yml", ".yaml")) or not isinstance(value, str):
                    continue
                try:
                    yaml.load(value, Loader=_AppTagLoader)
                except yaml.YAMLError as exc:
                    return f"invalid embedded YAML in {field}.{key}: {exc}"
    return None


class _StrictKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects a duplicate mapping key instead of letting the last one win.

    Plain YAML treats a repeated key as an overwrite: the document stays valid, kubectl
    applies it, and only the final value takes effect. That is how homepage ended up with
    both `automountServiceAccountToken: true` (needed by its kubernetes widget) and a
    `false` inherited from the estate-wide 02e9cfac sweep in one pod spec — the widget would
    have gone dark with every check green. A rebase or a merge that lands two edits in the
    same block is the way this arrives, so it needs catching at render time, not by reading.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key {key!r} — the later value silently wins",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


class _AppTagLoader(_StrictKeyLoader):
    """SafeLoader that tolerates application-defined tags in EMBEDDED config (home-assistant's
    configuration.yaml uses ``!include``/``!secret``). The structure is still fully parsed —
    only the tag resolves to a placeholder. HA's own files get deep validation from
    validate-ha-config; this guard just proves the ConfigMap embeds well-formed YAML."""


_AppTagLoader.add_multi_constructor("!", lambda loader, suffix, node: f"<{suffix}>")


def _from_json(value) -> object:
    """Ansible's ``from_json`` for looked-up templates (home-assistant's secrets.yaml.j2 parses
    a SOPS-stored service-account blob). Under this guard the value is a StubUndefined, not
    JSON — return an empty dict so attribute access on the result stubs out like any other
    undefined instead of aborting the render."""
    try:
        return json.loads(str(value))
    except ValueError, TypeError:
        return {}


def _to_json(value) -> str:
    """Ansible's ``to_json`` for looked-up templates.

    ``default=str`` so a StubUndefined serializes as its placeholder instead of aborting the render.
    """
    return json.dumps(value, default=str)


def make_lookup(ctx: dict):
    """Minimal stand-in for Ansible's ``lookup``, supporting the ``file`` and ``template`` plugins.

    A ConfigMap that embeds a config file the Docker role already owns reads it with a lookup
    rather than keeping a second copy. Stubbing the result would let a malformed embed through,
    so the real file is read here — the whole point of this guard is that what renders in CI is
    what a deploy renders.

    ``template`` needs the render context, hence the closure: livesync's CouchDB local.ini is a
    Jinja template on the Docker side, and reading it with ``file`` would leave any variable
    added to it later embedded as literal ``{{ ... }}`` in the ConfigMap.
    """

    def lookup(kind: str, *args: str) -> str:
        path = Path(args[0])
        if kind == "file":
            return path.read_text().rstrip("\n")
        if kind == "pipe":
            # Only the binary-embed idiom, done hermetically in Python rather than by
            # running a shell: lookup('file') utf-8-decodes and would mangle binary, so
            # templates embedding images (homepage's icons ConfigMap) pipe base64 instead.
            cmd = args[0].split()
            if cmd[:2] == ["base64", "-w0"] and len(cmd) == 3:
                return base64.b64encode(Path(cmd[2]).read_bytes()).decode()
            raise ValueError(
                f"lookup('pipe') is only supported for 'base64 -w0 <path>', got {args[0]!r}"
            )
        if kind == "template":
            env = make_env([path.parent])
            env.globals["lookup"] = lookup
            env.filters["from_json"] = _from_json
            env.filters["to_json"] = _to_json
            return env.get_template(path.name).render(ctx).rstrip("\n")
        raise ValueError(
            "validate_k8s_manifests implements lookup('file') and lookup('template'), "
            f"got {kind!r}"
        )

    return lookup


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


CRD_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


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


# Traefik's TLS entrypoint in this cluster. `websecure` is upstream's conventional name and is
# not used here — see the traefik role's static-config.yaml.j2.
HTTPS_ENTRYPOINT = "https"


def https_route_without_tls(doc: dict) -> str | None:
    """An IngressRoute on the https entrypoint that declares no `spec.tls`.

    Such a route is not a TLS router at all: Traefik never considers it a candidate for an
    HTTPS request, so the route silently never matches while reading as correctly configured.
    ansible/templates/ingressroute.yml.j2 already says the `tls:` key is never conditional and
    records that omitting it cost a week — this makes the rule something a machine enforces
    rather than a comment a hand-written route can miss.

    IngressRoute is a CRD, so `schema_error` returns NO_SCHEMA for it and nothing else here
    looks at one. `certResolver` may legitimately be absent (an empty resolver means Traefik
    serves its own self-signed certificate); the `tls` key itself may not.
    """
    if doc.get("kind") != "IngressRoute":
        return None
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return None
    entrypoints = spec.get("entryPoints")
    if not isinstance(entrypoints, list) or HTTPS_ENTRYPOINT not in entrypoints:
        return None
    if "tls" in spec:
        return None
    name = (doc.get("metadata") or {}).get("name", "<unnamed>")
    return (
        f"IngressRoute {name} lists the {HTTPS_ENTRYPOINT} entrypoint but declares no "
        "spec.tls, so Traefik never treats it as a TLS router and the route never matches. "
        "Add `tls:` — certResolver stays optional, the key does not."
    )


def workload_container_ports(docs) -> dict[tuple, set[int]]:
    """Pod-label-set -> the container ports its workloads actually listen on.

    Keyed by the frozen label set on `spec.template.metadata.labels`, which is what a
    NetworkPolicy's podSelector matches against.
    """
    ports: dict[tuple, set[int]] = {}
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") not in (
            "Deployment",
            "DaemonSet",
            "StatefulSet",
        ):
            continue
        template = ((doc.get("spec") or {}).get("template")) or {}
        labels = (template.get("metadata") or {}).get("labels")
        if not isinstance(labels, dict):
            continue
        key = tuple(sorted((str(k), str(v)) for k, v in labels.items()))
        found = ports.setdefault(key, set())
        for container in (template.get("spec") or {}).get("containers") or []:
            for port in (container or {}).get("ports") or []:
                number = (port or {}).get("containerPort")
                if isinstance(number, int):
                    found.add(number)
    return ports


def _selected_ports(
    selector: dict, by_labels: dict[tuple, set[int]]
) -> set[int] | None:
    """The container ports of every workload a podSelector matches, or None when it matches none.

    None and an empty set mean different things: no matching workload is no evidence, while a
    matching workload with no declared containerPort is a workload we cannot check.
    """
    wanted = selector.get("matchLabels")
    if not isinstance(wanted, dict) or not wanted:
        return None
    matched = None
    for labels, ports in by_labels.items():
        as_dict = dict(labels)
        if all(as_dict.get(str(k)) == str(v) for k, v in wanted.items()):
            matched = (matched or set()) | ports
    return matched


def service_port_translations(docs) -> dict[int, set[int]]:
    """Service port -> the targetPorts it forwards to, for every Service that translates.

    Only ports where `port` and `targetPort` differ are recorded. A Service that maps 1:1
    cannot produce the confusion this rule exists to catch.
    """
    translations: dict[int, set[int]] = {}
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "Service":
            continue
        for port in ((doc.get("spec") or {}).get("ports")) or []:
            published, target = (port or {}).get("port"), (port or {}).get("targetPort")
            if (
                isinstance(published, int)
                and isinstance(target, int)
                and published != target
            ):
                translations.setdefault(published, set()).add(target)
    return translations


def netpol_port_mismatches(
    doc: dict, by_labels: dict[tuple, set[int]], translations: dict[int, set[int]]
) -> list[str]:
    """NetworkPolicy ports that hold a Service's published port instead of the pod's.

    A NetworkPolicy port is the CONTAINER's port, never the Service's. Traefik is the standing
    example: its Service publishes 80 and 443 while the pod listens on 8000 and 8443, so a
    policy written against the Service's numbers fences nothing — silently, because a policy
    that matches no traffic looks exactly like one that matches all of it until something is
    refused.

    THREE conditions, all required, because the obvious one-condition version is wrong. Flagging
    every policy port that is not a declared containerPort produces a false positive on the
    first real workload: a containerPort declaration is informational, and traefik's own
    Prometheus metrics listener answers on 8080 while declaring nothing. So the port must also
    be a port some Service publishes, and that Service must translate it to a DIFFERENT
    targetPort — which is exactly the confusion, and nothing else.
    """
    if doc.get("kind") != "NetworkPolicy":
        return []
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return []
    selector = spec.get("podSelector")
    if not isinstance(selector, dict):
        return []
    declared = _selected_ports(selector, by_labels)
    if not declared:
        # No matching workload, or one that declares no containerPort: no evidence either way.
        return []
    name = (doc.get("metadata") or {}).get("name", "<unnamed>")
    problems = []
    for rule in spec.get("ingress") or []:
        for port in (rule or {}).get("ports") or []:
            number = (port or {}).get("port")
            if not isinstance(number, int) or number in declared:
                continue
            targets = translations.get(number)
            if not targets:
                continue
            problems.append(
                f"NetworkPolicy {name} admits port {number}, which is a Service's published "
                f"port forwarding to {sorted(targets)}. A NetworkPolicy port is the "
                f"container's port — the pods this policy selects listen on {sorted(declared)}."
            )
    return problems


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
    if err:
        return err, []

    err = yaml_error(rendered)
    if err:
        print(f"\n----- rendered {role}/{tpl.name} -----", file=sys.stderr)
        dump_numbered(rendered)
        return err, []

    return None, parse_docs(rendered)


def main() -> int:
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
    for role in roles:
        # Not every .j2 in a k8s role's templates/ is a manifest — a role may also ship a
        # helper script (claude-otel's telemetry-health.sh.j2) or a Dockerfile for
        # image-builder (homelab-mcp). Shell is rendered and linted by
        # validate_shell_templates.py; a Dockerfile is consumed by buildctl. Parsing
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
        pvc_names.update(volume_claim_pvc_names(role, ctx))
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
