"""Pure, offline-testable logic for the homelab-mcp server.

Everything that decides what leaves the box lives here — the container-field
whitelist, the file-read jail, the path denylist, and the bearer-token check —
so it can be unit-tested without a running MCP server or any network. app.py is
the thin FastMCP wiring on top; the risky decisions are all in this module.
"""

from __future__ import annotations

import hmac
from pathlib import Path, PurePosixPath

# docker-proxy runs CONTAINERS=1, so GET /containers/{id}/json returns the
# container's full Config incl. Env (its secrets) and haproxy can't body-filter.
# The MCP must never pass those through: build the reply from an explicit
# allowlist of non-secret fields instead of redacting a copy of the raw inspect.
_STATE_FIELDS = ("Status", "Running", "StartedAt", "FinishedAt", "ExitCode")


def strip_container_fields(inspect: dict) -> dict:
    """Reduce a docker inspect object to non-secret status fields only.

    Reads named keys out of the raw inspect; never spreads it. In particular
    Config.Env, Mounts, and the raw Config are dropped entirely.
    """
    state = inspect.get("State") or {}
    health = state.get("Health") or {}
    config = inspect.get("Config") or {}
    out = {
        "name": (inspect.get("Name") or "").lstrip("/"),
        "image": config.get("Image"),
        "restart_count": inspect.get("RestartCount"),
        "created": inspect.get("Created"),
        "health": health.get("Status"),
    }
    for key in _STATE_FIELDS:
        out[key.lower() if key != "ExitCode" else "exit_code"] = state.get(key)
    return out


def summarize_container_list(items: list[dict]) -> list[dict]:
    """Summarize GET /containers/json rows. This endpoint omits Env, but we
    still project an explicit allowlist rather than trusting that."""
    rows = []
    for it in items:
        names = it.get("Names") or []
        rows.append(
            {
                "name": (names[0] if names else "").lstrip("/"),
                "image": it.get("Image"),
                "state": it.get("State"),
                "status": it.get("Status"),
            }
        )
    return rows


def parse_metric(resp: dict) -> list[dict]:
    """Flatten a Prometheus instant-query response to {labels, value} rows."""
    result = (resp.get("data") or {}).get("result") or []
    rows = []
    for series in result:
        value = series.get("value") or [None, None]
        rows.append({"metric": series.get("metric") or {}, "value": value[-1]})
    return rows


def parse_targets(resp: dict) -> list[dict]:
    """Reduce Prometheus /api/v1/targets to health per target."""
    active = (resp.get("data") or {}).get("activeTargets") or []
    rows = []
    for t in active:
        labels = t.get("labels") or {}
        rows.append(
            {
                "job": labels.get("job"),
                "instance": labels.get("instance"),
                "health": t.get("health"),
                "last_error": t.get("lastError") or "",
            }
        )
    return rows


def parse_loki(resp: dict) -> list[dict]:
    """Flatten a Loki query_range response to {stream, ts, line} rows."""
    result = (resp.get("data") or {}).get("result") or []
    rows = []
    for stream in result:
        labels = stream.get("stream") or {}
        for ts, line in stream.get("values") or []:
            rows.append({"labels": labels, "ts": ts, "line": line})
    return rows


# --- Downstream URL-path guards ---------------------------------------------
# `name` (docker) and `entity_id` (Home Assistant) get interpolated into an
# outbound URL path, and httpx does not percent-encode an f-string path segment.
# A value carrying `/`, `?`, `..`, or `%` would steer the request to a different
# endpoint (e.g. the container-logs tool onto the inspect route, which leaks Env).
# Constrain each to the character set its downstream actually uses before it
# reaches the URL.
def container_ref_valid(name: str) -> bool:
    """True if `name` is a bare docker container name/id (no URL-path characters)."""
    return (
        bool(name)
        and name[0].isascii()
        and name[0].isalnum()
        and all(c.isascii() and (c.isalnum() or c in "_.-") for c in name)
    )


def _is_ha_slug(part: str) -> bool:
    """True for a Home Assistant identifier segment — ascii [a-z0-9_], non-empty."""
    return bool(part) and all(
        c.isascii() and (c.islower() or c.isdigit() or c == "_") for c in part
    )


def entity_id_valid(entity_id: str) -> bool:
    """True if `entity_id` is a bare Home Assistant `domain.object_id` slug."""
    domain, dot, obj = entity_id.partition(".")
    return dot == "." and "." not in obj and _is_ha_slug(domain) and _is_ha_slug(obj)


# --- File-read jail ---------------------------------------------------------
# The container bind-mounts ansible/ read-only; these guards keep a read_file
# tool inside it. The age private key lives outside the mount (~/.config/sops),
# so the worst a jail bug could leak is ansible/ content — and secrets.yml is
# SOPS ciphertext, additionally denied by name below.
_DENIED_BASENAMES = {"secrets.yml", "secrets.yaml", "keys.txt"}
_DENIED_PARTS = {"secrets"}
_DENIED_SUFFIXES = (".age", ".agekey", ".key", ".pem", ".p12", ".pfx")


def path_is_denied(rel: str) -> bool:
    """True if any path component names a secret file/dir or key material."""
    for part in PurePosixPath(rel).parts:
        low = part.lower()
        if low in _DENIED_BASENAMES or low in _DENIED_PARTS:
            return True
        if low.endswith(_DENIED_SUFFIXES):
            return True
    return False


def resolve_within_jail(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root`, rejecting escapes.

    Path.resolve() collapses `..` and follows symlinks, so a link pointing out
    of the jail is caught by the containment check too.
    """
    if "\x00" in rel:
        raise ValueError("path contains null byte")
    if PurePosixPath(rel).is_absolute():
        raise ValueError("absolute paths are not allowed")
    root_resolved = root.resolve()
    candidate = (root_resolved / rel).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError("path escapes the jail")
    # Re-check the denylist on the RESOLVED path. read_file runs path_is_denied on
    # the pre-resolution name only, so a benignly-named symlink pointing at a denied
    # file inside the jail (e.g. -> vars/secrets.yml) would otherwise slip through.
    if candidate != root_resolved and path_is_denied(
        str(candidate.relative_to(root_resolved))
    ):
        raise ValueError("path resolves to a denied file")
    return candidate


def bearer_token_valid(header: str | None, expected: str) -> bool:
    """Constant-time check of an `Authorization: Bearer <token>` header."""
    prefix = "Bearer "
    if not expected or not header or not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix) :], expected)


def allowed_hosts_and_origins(public_host: str) -> tuple[list[str], list[str]]:
    """Trusted Host/Origin allowlist for the MCP transport's DNS-rebinding guard.

    `public_host` is the external hostname Traefik forwards (e.g. mcp.local.<domain>).
    Without it the transport's default guard rejects any non-localhost Host header with
    a 421 before auth is ever reached.
    """
    if not public_host:
        return [], []
    hosts = [public_host, "127.0.0.1:8000", "localhost:8000"]
    origins = [f"https://{public_host}"]
    return hosts, origins
