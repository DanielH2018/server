#!/usr/bin/env python3
"""Render every configured container's docker-compose.yml.j2 and assert the
output parses as valid YAML.

This guards against template edits — especially to the shared ``traefik.yml.j2``
and ``autokuma.yml.j2`` label macros — that silently produce malformed YAML or
broken indentation. It renders structure, not values: secrets and other runtime
variables are stubbed, so no access to the SOPS-encrypted ``secrets.yml`` is
needed.

The container set and per-service parameters are taken from the real
``containers_list`` in each ``inventory/host_vars/*.yml`` file, so each template
is exercised with the same shape it is deployed with (port, hostname, networks,
use_authelia). Commented-out services are skipped automatically (they are not in
the parsed list).

Run directly (``python3 scripts/validate_compose_templates.py``) or via the
``validate-compose-templates`` prek hook. Exits non-zero if any template fails to
render or produces invalid YAML.
"""

from __future__ import annotations

import hashlib
import sys

import yaml
from jinja2 import Environment

from _render_guard import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    HOST_VARS,
    SHARED_TPL,
    dump_numbered,
    load_yaml,
    make_env,
    render_or_error,
)


def _ansible_hash(value, algo="sha1"):
    """Mirror Ansible's `hash` filter so templates using it render identically here."""
    return hashlib.new(algo, str(value).encode("utf-8")).hexdigest()


ROLES = ANSIBLE / "roles" / "containers"


def build_env(role: str) -> Environment:
    env = make_env([ROLES / role / "templates", SHARED_TPL])
    # Used by the jittered healthcheck interval inlined in roles/containers/dozzle. It came
    # from a shared healthcheck.yml.j2 macro that no longer exists; the filter is still needed.
    env.filters["hash"] = _ansible_hash
    return env


def _unescaped_dollars(value) -> list[str]:
    """From a string or list-of-strings, return the items containing a `$` that is
    NOT doubled as `$$`. Dropping every `$$` first means an escaped `$$(...)` leaves
    no `$` behind, while a lone `$VAR` / `$(...)` does."""
    items = value if isinstance(value, list) else [value]
    return [s for s in items if isinstance(s, str) and "$" in s.replace("$$", "")]


def find_dollar_escape_bugs(docs) -> list[tuple[str, str, str]]:
    """Return (service, key, snippet) for every command/entrypoint/healthcheck.test
    string holding an un-doubled `$`. Docker Compose interpolates `$VAR` / `${VAR}` /
    `$(...)` at parse time, so a shell `$` meant for the container must be written
    `$$`; otherwise the value is silently blanked or substituted. Restricted to these
    shell-bearing keys so the deliberate `${GID-...}` interpolation that some services
    use in `environment:` is not flagged. The plain-YAML validator and ansible-lint
    both miss this."""
    bugs: list[tuple[str, str, str]] = []
    for doc in docs:
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            continue
        for svc, spec in services.items():
            if not isinstance(spec, dict):
                continue
            for key in ("command", "entrypoint"):
                if key in spec:
                    bugs += [(svc, key, s) for s in _unescaped_dollars(spec[key])]
            hc = spec.get("healthcheck")
            if isinstance(hc, dict) and "test" in hc:
                bugs += [
                    (svc, "healthcheck.test", s) for s in _unescaped_dollars(hc["test"])
                ]
    return bugs


def find_watchtower_label_bugs(docs) -> list[tuple[str, str]]:
    """Return (service, label) for every LIST-form ``com.centurylinklabs.watchtower.*``
    label written without an ``=``. Docker splits a list-item label on the first ``=``
    only, so a ``:``-separated watchtower label (e.g. ``...depends-on:docker-proxy``)
    parses as a key with an EMPTY value — the directive (``enable`` / ``depends-on``)
    silently becomes a no-op. The plain-YAML validator and ansible-lint both miss this
    because the document still renders and parses cleanly. Mapping-form labels are
    inherently ``key: value`` so they need no ``=`` and are skipped."""
    bugs: list[tuple[str, str]] = []
    for doc in docs:
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            continue
        for svc, spec in services.items():
            if not isinstance(spec, dict):
                continue
            labels = spec.get("labels")
            if not isinstance(labels, list):
                continue
            for label in labels:
                if (
                    isinstance(label, str)
                    and label.startswith("com.centurylinklabs.watchtower.")
                    and "=" not in label
                ):
                    bugs.append((svc, label))
    return bugs


# Documented exceptions to the cap_drop: [ALL] policy (service -> why). Keep SMALL and
# justified — the baseline expectation is that EVERY service drops all caps and adds back only
# what it proves it needs. These three need broad host/device access by design (verified in the
# security reviews); everything else drops ALL.
CAP_DROP_EXEMPT: dict = {
    "cadvisor": "needs host-wide introspection (cgroups/proc, SYS_PTRACE) to read every container's stats",
    "scrutiny-web": "LSIO web UI verified to need its default caps; no-cap_drop is accepted/documented",
    "scrutiny-collector": "SMART collector runs smartctl against raw block devices (needs SYS_RAWIO/SYS_ADMIN)",
}

# Companion to CAP_DROP_EXEMPT for the `security_opt: [no-new-privileges:true]` baseline. Every
# service should set it (it blocks a setuid binary from re-gaining a dropped capability); a service
# that legitimately can't goes here with a reason. Kept empty until a real exception appears — the
# whole fleet sets it today, and the guard exists so a new service (or a copy-paste that silently
# drops the line, the way ical-proxy's indent drifted) can't omit it unnoticed.
NO_NEW_PRIV_EXEMPT: dict = {}

# The mutable-tag update-policy guard (WATCHTOWER_AUTOUPDATE, find_undeclared_update_policy,
# find_autoupdate_optout_conflicts) was removed on 2026-08-15: watchtower retired 2026-08-09,
# so nothing auto-updates any more. `docker_deploy.yml` deploys with `pull: policy`, which
# never re-pulls a mutable tag already present locally, so a `latest` tag now ages in place
# rather than drifting — the risk the guard existed to catch has no actor. An image refresh is
# the deliberate `deploy.yml --tags <svc> -e common_pull=always`.


def _cap_drops_all(spec: dict) -> bool:
    caps = spec.get("cap_drop")
    return isinstance(caps, list) and any(
        isinstance(c, str) and c.upper() == "ALL" for c in caps
    )


def find_missing_cap_drop(docs, exempt=frozenset()) -> list:
    """Return service names that do NOT ``cap_drop: [ALL]`` and aren't in ``exempt``. Drop-all
    is the hardening baseline (then add back minimal caps); a service that drops nothing — or
    only a subset — keeps Docker's default capability set."""
    missing = []
    for doc in docs:
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            continue
        for svc, spec in services.items():
            if (
                isinstance(spec, dict)
                and svc not in exempt
                and not _cap_drops_all(spec)
            ):
                missing.append(svc)
    return missing


def _sets_no_new_privileges(spec: dict) -> bool:
    opts = spec.get("security_opt")
    return isinstance(opts, list) and any(
        isinstance(o, str) and o.replace(" ", "") == "no-new-privileges:true"
        for o in opts
    )


def find_missing_no_new_privileges(docs, exempt=frozenset()) -> list:
    """Return service names that do NOT set ``security_opt: [no-new-privileges:true]`` and aren't
    in ``exempt``. It's the companion baseline to ``cap_drop: [ALL]`` — stops a setuid binary from
    re-escalating past the dropped caps — so it's enforced symmetrically here; a service that omits
    it (a new one, or a copy-paste that drops the line) is flagged unless allowlisted with a reason."""
    missing = []
    for doc in docs:
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            continue
        for svc, spec in services.items():
            if (
                isinstance(spec, dict)
                and svc not in exempt
                and not _sets_no_new_privileges(spec)
            ):
                missing.append(svc)
    return missing


def check_container(host_ctx: dict, ci: dict) -> str | None:
    """Render one container template; return an error string or None on success."""
    name = ci.get("name")
    if not name:
        return None
    role = ROLES / name
    if not role.is_dir():
        return (
            f"no role at ansible/roles/containers/{name} — the inventory entry names a role "
            "that "
            "does not exist (moved, renamed, or retired without updating containers_list)"
        )
    tpl = role / "templates" / "docker-compose.yml.j2"
    if not tpl.exists():
        return (
            f"role exists but has no ansible/roles/containers/{name}/templates/"
            "docker-compose.yml.j2 — a Docker entry must render a compose file"
        )

    env = build_env(name)
    ctx = {**host_ctx, "container_item": ci}
    rendered, err = render_or_error(env, "docker-compose.yml.j2", ctx)
    if err:
        return err

    try:
        docs = list(yaml.safe_load_all(rendered))
    except yaml.YAMLError as exc:
        print(f"\n----- rendered {name}/docker-compose.yml.j2 -----", file=sys.stderr)
        dump_numbered(rendered)
        return f"invalid YAML: {exc}"

    bugs = find_dollar_escape_bugs(docs)
    if bugs:
        detail = "; ".join(
            f"{svc}.{key}: {snippet.strip()[:80]}" for svc, key, snippet in bugs
        )
        return f"un-escaped '$' (Compose will interpolate it — double it to '$$'): {detail}"

    wt_bugs = find_watchtower_label_bugs(docs)
    if wt_bugs:
        detail = "; ".join(f"{svc}: {label}" for svc, label in wt_bugs)
        return (
            "watchtower label missing '=' (Docker stores it as a key with an empty value, "
            f"so the directive is a silent no-op — use '='): {detail}"
        )

    cap_missing = find_missing_cap_drop(docs, CAP_DROP_EXEMPT)
    if cap_missing:
        return (
            "missing `cap_drop: [ALL]` (drop all caps, add back only what's needed — or "
            f"allowlist in CAP_DROP_EXEMPT with a reason): {', '.join(cap_missing)}"
        )

    nnp_missing = find_missing_no_new_privileges(docs, NO_NEW_PRIV_EXEMPT)
    if nnp_missing:
        return (
            "missing `security_opt: [no-new-privileges:true]` (companion of cap_drop: [ALL] — "
            f"blocks setuid re-escalation; allowlist in NO_NEW_PRIV_EXEMPT with a reason): "
            f"{', '.join(nnp_missing)}"
        )

    return None


def main() -> int:
    all_vars = load_yaml(ALL_VARS)
    host_files = sorted(HOST_VARS.glob("*.yml"))
    if not host_files:
        print(f"No host_vars found under {HOST_VARS}", file=sys.stderr)
        return 1

    failures = 0
    checked = 0
    for host_file in host_files:
        host_vars = load_yaml(host_file)
        containers = host_vars.get("containers_list") or []
        # host scalars (domain, server_ip, kuma_docker_host, ...) override the base.
        host_ctx = {**BASE_CONTEXT, **all_vars, **host_vars}
        host_ctx.pop("containers_list", None)

        # k8s entries render manifests, not compose — validate_k8s_manifests.py owns them.
        # Excluding them here is what lets a *Docker* entry with no template be an error
        # rather than a silent [ok]; that blind spot let a broken glances role ship.
        docker = [ci for ci in containers if ci.get("platform") != "k8s"]
        print(f"== {host_file.name} ({len(docker)} Docker services) ==")
        for ci in docker:
            err = check_container(host_ctx, ci)
            checked += 1
            name = ci.get("name", "<unnamed>")
            if err:
                failures += 1
                print(f"  [FAIL] {name}: {err}", file=sys.stderr)
            else:
                print(f"  [ok]   {name}")

    print(f"\n{checked} template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
