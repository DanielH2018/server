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

K8S_ROLES = ANSIBLE / "roles" / "k8s"
HOST_VARS = ANSIBLE / "inventory" / "host_vars" / "daniel-box.yml"
# Helper roles, included by service roles rather than deployed on their own. They have no
# containers_list entry because they are not services, so the platform check below would
# always fail for them. seed-volume's templates still render — they just render with vars the
# calling role supplies, not from an inventory entry.
SKIP_ROLES = {"manifests", "seed-volume"}


def k8s_entries() -> dict[str, dict]:
    """containers_list entries for the k8s platform, keyed by service name."""
    entries = load_yaml(HOST_VARS).get("containers_list") or []
    return {c["name"]: c for c in entries if c.get("platform") == "k8s"}


def role_defaults(role: str) -> dict:
    return load_yaml(K8S_ROLES / role / "defaults" / "main.yml")


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
                    yaml.safe_load(value)
                except yaml.YAMLError as exc:
                    return f"invalid embedded YAML in {field}.{key}: {exc}"
    return None


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
        if kind == "template":
            env = make_env([path.parent])
            env.globals["lookup"] = lookup
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
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), "playbook_dir": str(ANSIBLE)}
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

        ctx = {**base, **role_defaults(role), "container_item": entries[role]}
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
