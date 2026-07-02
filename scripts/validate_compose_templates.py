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

import sys
import hashlib
import re
from pathlib import Path

import yaml
from jinja2 import ChainableUndefined, Environment, FileSystemLoader


def _ansible_hash(value, algo="sha1"):
    """Mirror Ansible's `hash` filter so templates using it render identically here."""
    return hashlib.new(algo, str(value).encode("utf-8")).hexdigest()


REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "ansible"
SHARED_TPL = ANSIBLE / "templates"  # traefik.yml.j2 / autokuma.yml.j2 live here
ROLES = ANSIBLE / "roles" / "containers"
HOST_VARS = ANSIBLE / "inventory" / "host_vars"
ALL_VARS = ANSIBLE / "inventory" / "group_vars" / "all.yml"

# Fallback values for variables that are not defined in the (plaintext) inventory
# — e.g. host facts. Anything still missing (SOPS secrets) renders via StubUndefined.
BASE_CONTEXT = {
    "docker_network": "proxy",
    "puid": 1000,
    "pgid": 1000,
    "tz": "America/Chicago",
    "sys_user": "ubuntu",
    "email": "stub@example.com",
    "domain": "example.com",
    "server_ip": "10.0.0.1",
    "kuma_docker_host": 1,
}


class StubUndefined(ChainableUndefined):
    """Any undefined variable (a SOPS secret, a host fact) renders as the literal
    ``STUB`` and tolerates attribute/item access and iteration, so structural
    rendering never aborts on a missing value."""

    _FILL = "STUB"

    def __str__(self) -> str:  # {{ secret }}
        return self._FILL

    def __iter__(self):  # {% for x in undefined %}
        return iter(())


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def build_env(role: str) -> Environment:
    role_tpl_dir = ROLES / role / "templates"
    env = Environment(
        loader=FileSystemLoader([str(role_tpl_dir), str(SHARED_TPL)]),
        undefined=StubUndefined,
        # Match Ansible's Templar so rendered whitespace matches a real deploy.
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    env.filters["hash"] = _ansible_hash  # used by healthcheck.yml.j2's interval jitter
    return env


def dump_numbered(text: str) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        print(f"  {i:3d}| {line}", file=sys.stderr)


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

# The INTENTIONAL watchtower auto-update pool: mutable-tag services deliberately left for
# watchtower to update unattended (disposable / stateless / trivially rolled back). A new
# mutable-tag service is flagged until it either opts out (watchtower.enable=false) or is added
# here on purpose — so the karakeep/janitorr "stateful service silently swept into auto-update"
# drift can't recur. Version-pinned tags need no entry. Curated from the real render.
# A service may not be BOTH here and opted out — find_autoupdate_optout_conflicts fails that.
WATCHTOWER_AUTOUPDATE: frozenset = frozenset(
    {
        # Infra/monitoring sidecars + stateless/disposable services on rolling tags, intentionally
        # kept current by watchtower (the critical/stateful tier is version-pinned + Renovate-managed
        # instead — those use immutable tags so they don't appear here).
        "autoheal",
        "autokuma",
        "watchtower",
        "dozzle",
        "glances",
        "ddns-direct",
        "ddns-proxied",
        "wg-easy",
        "peanut",
        "grafana",
        "loki",
        "promtail",
        "prometheus",
        "node-exporter",
        "homepage",
        "healthchecks",
        "bento-pdf",
        "littlelink",
        "speedtest",
        "code-server",
        "freshrss",
        "terraria",
        # influxdb:2.9 — non-critical SMART-history time-series store (scrutiny role);
        # documented to stay on a pinned-major tag with watchtower patching within 2.9
        # (see scrutiny/CLAUDE.md), unlike the critical/stateful tier above it.
        "scrutiny-influxdb",
    }
)

# Channel tags whose content changes under the same string (vs a version-bearing tag).
_MUTABLE_TAGS = {
    "latest",
    "release",
    "stable",
    "main",
    "master",
    "dev",
    "edge",
    "nightly",
    "rolling",
}


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


_BARE_MAJOR_TAG = re.compile(r"^v?\d+(\.\d+)?$")


def _is_mutable_tag(image: str) -> bool:
    """True if the image reference uses a mutable channel tag (content can change under the same
    string): untagged (implicit :latest), latest/release/stable/main/..., a channel word joined
    to a component by a hyphen on EITHER side — a ``-stable`` suffix (jvm-stable) OR a ``master-``
    prefix (master-web/master-collector) — or a bare-major/major.minor numeric tag (``2``, ``3``,
    ``3.5``) that upstream re-points at every new release under the same string (e.g. couchdb:3,
    eclipse-mosquitto:2). A fully version-bearing tag (1.2.3, v1.41.0, 2026.05.0, ...-lsNN) is
    not — three-plus numeric components pin an exact release."""
    ref = image.split("@", 1)[0]  # drop any digest
    # repo:tag split on the LAST colon, unless that colon is a registry port (has a '/' after)
    if ":" in ref and "/" not in ref.rsplit(":", 1)[1]:
        tag = ref.rsplit(":", 1)[1]
    else:
        tag = ""  # no tag -> implicit :latest
    if tag == "":
        return True
    low = tag.lower()
    return (
        low in _MUTABLE_TAGS
        or any(low.endswith("-" + m) for m in _MUTABLE_TAGS)
        or any(low.startswith(m + "-") for m in _MUTABLE_TAGS)
        or bool(_BARE_MAJOR_TAG.match(low))
    )


def _has_watchtower_optout(spec: dict) -> bool:
    labels = spec.get("labels")
    key = "com.centurylinklabs.watchtower.enable"
    if isinstance(labels, list):
        return any(
            isinstance(lbl, str) and lbl.replace(" ", "") == key + "=false"
            for lbl in labels
        )
    if isinstance(labels, dict):
        return str(labels.get(key, "")).strip().lower() == "false"
    return False


def find_undeclared_update_policy(docs, autoupdate=frozenset()) -> list:
    """Return service names on a MUTABLE image tag that have neither opted out of watchtower
    (enable=false) nor been declared in ``autoupdate`` — forcing an explicit update-policy
    choice so a stateful service can't be silently swept into watchtower's auto-update pool."""
    undeclared = []
    for doc in docs:
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            continue
        for svc, spec in services.items():
            if not isinstance(spec, dict) or svc in autoupdate:
                continue
            image = spec.get("image")
            if (
                isinstance(image, str)
                and _is_mutable_tag(image)
                and not _has_watchtower_optout(spec)
            ):
                undeclared.append(svc)
    return undeclared


def find_autoupdate_optout_conflicts(docs, autoupdate=frozenset()) -> list:
    """Return service names BOTH declared in ``autoupdate`` AND carrying the watchtower opt-out
    label. The two contradict (the label wins at runtime), and the stale allowlist entry
    short-circuits ``find_undeclared_update_policy`` — so if the label is ever dropped in a
    refactor the service silently rejoins the auto-update pool with no CI signal."""
    conflicts = []
    for doc in docs:
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            continue
        for svc, spec in services.items():
            if (
                isinstance(spec, dict)
                and svc in autoupdate
                and _has_watchtower_optout(spec)
            ):
                conflicts.append(svc)
    return conflicts


def check_container(host_ctx: dict, ci: dict) -> str | None:
    """Render one container template; return an error string or None on success."""
    name = ci.get("name")
    if not name:
        return None
    tpl = ROLES / name / "templates" / "docker-compose.yml.j2"
    if not tpl.exists():
        return None  # role has no compose template (nothing to validate)

    env = build_env(name)
    ctx = {**host_ctx, "container_item": ci}
    env.globals.update(ctx)
    try:
        rendered = env.get_template("docker-compose.yml.j2").render(**ctx)
    except Exception as exc:  # noqa: BLE001 — surface any render failure
        return f"render error: {type(exc).__name__}: {exc}"

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

    undeclared = find_undeclared_update_policy(docs, WATCHTOWER_AUTOUPDATE)
    if undeclared:
        return (
            "mutable image tag with no update-policy decision — add "
            "`com.centurylinklabs.watchtower.enable=false` to opt out (pinned/Renovate tier), "
            "or add the service to WATCHTOWER_AUTOUPDATE if it's intentionally auto-updated: "
            f"{', '.join(undeclared)}"
        )

    conflicts = find_autoupdate_optout_conflicts(docs, WATCHTOWER_AUTOUPDATE)
    if conflicts:
        return (
            "in WATCHTOWER_AUTOUPDATE but ALSO carries watchtower.enable=false — the label "
            "wins, so the allowlist entry is stale and would mask a later-dropped label; "
            f"remove from WATCHTOWER_AUTOUPDATE: {', '.join(conflicts)}"
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

        print(f"== {host_file.name} ({len(containers)} active services) ==")
        for ci in containers:
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
