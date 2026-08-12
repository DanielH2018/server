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

Structural check only: secrets are stubbed (StubUndefined), so no SOPS access is needed. Run
directly or via the ``validate-k8s-manifests`` prek hook. Exits non-zero on any render failure
or invalid YAML.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import yaml

from _render_guard import (
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
from toposort import filter_by_platform  # noqa: E402 — needs the path insert above

K8S_ROLES = ANSIBLE / "roles" / "k8s"
HOST_VARS = ANSIBLE / "inventory" / "host_vars" / "daniel-box.yml"
# Helper roles, included by service roles rather than deployed on their own. They have no
# containers_list entry because they are not services, so the platform check below would
# always fail for them. seed-volume's templates still render — they just render with vars the
# calling role supplies, not from an inventory entry.
# image-builder is the third of these: its Job and ConfigMap render from vars a caller passes
# (which image, which Dockerfile), so there is nothing to render standalone and no service to
# have an entry.
SKIP_ROLES = {"manifests", "seed-volume", "image-builder"}


def k8s_entries() -> dict[str, dict]:
    """containers_list entries for the k8s platform, keyed by service name."""
    entries = load_yaml(HOST_VARS).get("containers_list") or []
    return {c["name"]: c for c in entries if c.get("platform") == "k8s"}


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
    resolved = dict(values)
    for _ in range(passes):
        pending = {
            k: v for k, v in resolved.items() if isinstance(v, str) and "{{" in v
        }
        if not pending:
            break
        for key, value in pending.items():
            resolved[key] = env.from_string(value).render({**context, **resolved})
    return resolved


def role_defaults(role: str, base: dict) -> dict:
    return resolve_vars(load_yaml(K8S_ROLES / role / "defaults" / "main.yml"), base)


def yaml_error(rendered: str) -> str | None:
    """Return an error string if ``rendered`` is not parseable YAML, else None.

    Also parses YAML *embedded* in ConfigMap/Secret values. The manifest wrapping Traefik's
    static config and Authelia's configuration.yml is trivially valid whatever those blobs
    contain — they are opaque block scalars to the outer document — so checking only the
    outer YAML would miss precisely the indentation bugs that matter most here.
    """
    try:
        docs = list(yaml.safe_load_all(rendered))
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
                    yaml.load(value, Loader=_AppTagLoader)  # noqa: S506 — tolerant SafeLoader subclass
                except yaml.YAMLError as exc:
                    return f"invalid embedded YAML in {field}.{key}: {exc}"
    return None


class _AppTagLoader(yaml.SafeLoader):
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
    """Ansible's ``to_json`` for looked-up templates. ``default=str`` so a StubUndefined
    serializes as its placeholder instead of aborting the render."""
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


def check_template(role: str, tpl: Path, ctx: dict) -> str | None:
    """Render one manifest template; return an error string or None on success."""
    env = make_env([K8S_ROLES / role / "templates", SHARED_TPL])
    env.globals["lookup"] = make_lookup(ctx)
    # pihole's ConfigMap includes the shared dnsmasq template, which derives its override
    # records from the inventory via the repo's filter plugin — register the real thing.
    env.filters["filter_by_platform"] = filter_by_platform
    rendered, err = render_or_error(env, tpl.name, ctx)
    if err:
        return err

    err = yaml_error(rendered)
    if err:
        print(f"\n----- rendered {role}/{tpl.name} -----", file=sys.stderr)
        dump_numbered(rendered)
    return err


def main() -> int:
    # playbook_dir is real, not stubbed: templates use it to build lookup('file', ...) paths
    # into the Docker roles, and a stubbed value would make those paths unreadable.
    # group_vars values are resolved against each other first — several reference siblings
    # (k8s_registry_pull_host is "localhost:{{ k8s_registry_port }}"), and a role default that
    # reaches one of those needs it already expanded.
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)}
    base = resolve_vars(base, base)
    entries = k8s_entries()

    roles = sorted(
        d.name for d in K8S_ROLES.iterdir() if d.is_dir() and d.name not in SKIP_ROLES
    )
    checked = failures = 0
    for role in roles:
        # Not every .j2 in a k8s role's templates/ is a manifest — a role may also ship a
        # helper script (claude-otel's telemetry-health.sh.j2). Those are shell, not YAML, and
        # validate_shell_templates.py already renders and lints them; parsing one here reports
        # a bash comment as malformed YAML.
        templates = sorted(
            p
            for p in (K8S_ROLES / role / "templates").glob("*.j2")
            if not p.name.endswith(".sh.j2")
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

        ctx = {**base, **role_defaults(role, base), "container_item": entries[role]}
        for tpl in templates:
            checked += 1
            err = check_template(role, tpl, ctx)
            rel = f"{role}/{tpl.name}"
            if err:
                failures += 1
                print(f"  [FAIL] {rel}: {err}", file=sys.stderr)
            else:
                print(f"  [ok]   {rel}")

    print(f"\n{checked} k8s manifest template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
