"""homelab-mcp — read-only MCP server exposing this server's live state.

Streamable-HTTP MCP server. Every tool is a read; there is no code path that
starts/stops/execs a container, writes a file, or reads a secret. The Traefik
router already gates requests on a bearer token, but we re-check it here too so
a peer on the shared docker network can't reach the tools by skipping Traefik.

The decisions that matter (container-field whitelist, file jail, token check)
live in safe_reads.py and are unit-tested offline; this file is the wiring.
"""

from __future__ import annotations

import os
import ssl
import socket
from datetime import datetime, timezone
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse

import safe_reads

TOKEN = os.environ.get("HOMELAB_MCP_TOKEN", "")
HA_TOKEN = os.environ.get("HOMELAB_HA_TOKEN", "")
PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
LOKI = os.environ.get("LOKI_URL", "http://loki:3100")
DOCKER_PROXY = os.environ.get("DOCKER_PROXY_URL", "http://docker-proxy:2375")
SCRUTINY = os.environ.get("SCRUTINY_URL", "http://scrutiny:8080")
HA = os.environ.get("HA_URL", "http://home-assistant:8123")
FILE_ROOT = Path(os.environ.get("HOMELAB_FILE_ROOT", "/srv/ansible-src"))
FILE_MAX_BYTES = 256 * 1024
# The external hostname Traefik forwards. Must be allowlisted or the MCP transport's
# DNS-rebinding guard rejects the request with 421 before auth is ever checked.
PUBLIC_HOST = os.environ.get("MCP_PUBLIC_HOST", "")

_client = httpx.Client(timeout=httpx.Timeout(15.0))
_hosts, _origins = safe_reads.allowed_hosts_and_origins(PUBLIC_HOST)
mcp = FastMCP(
    "homelab",
    transport_security=(
        TransportSecuritySettings(allowed_hosts=_hosts, allowed_origins=_origins)
        if PUBLIC_HOST
        else None
    ),
)


def _get_json(
    url: str, params: dict | None = None, headers: dict | None = None
) -> dict:
    r = _client.get(url, params=params, headers=headers)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def query_metric(promql: str) -> list[dict]:
    """Prometheus instant query. Returns {metric labels, value} rows."""
    return safe_reads.parse_metric(
        _get_json(f"{PROMETHEUS}/api/v1/query", {"query": promql})
    )


@mcp.tool()
def query_logs(logql: str, limit: int = 100) -> list[dict]:
    """Loki range query. Returns {labels, ts, line} rows (newest range)."""
    resp = _get_json(
        f"{LOKI}/loki/api/v1/query_range", {"query": logql, "limit": limit}
    )
    return safe_reads.parse_loki(resp)


@mcp.tool()
def scrape_targets() -> list[dict]:
    """Prometheus scrape-target health (up/down + last error)."""
    return safe_reads.parse_targets(_get_json(f"{PROMETHEUS}/api/v1/targets"))


@mcp.tool()
def list_containers() -> list[dict]:
    """All containers with state/status/image (no secrets)."""
    items = _get_json(f"{DOCKER_PROXY}/containers/json", {"all": "1"})
    return safe_reads.summarize_container_list(items)


@mcp.tool()
def container_status(name: str) -> dict:
    """Status/health/uptime for one container (no Env, no secrets)."""
    return safe_reads.strip_container_fields(
        _get_json(f"{DOCKER_PROXY}/containers/{name}/json")
    )


@mcp.tool()
def service_health(name: str) -> dict:
    """Whether a container is running and, if it has a healthcheck, healthy."""
    data = safe_reads.strip_container_fields(
        _get_json(f"{DOCKER_PROXY}/containers/{name}/json")
    )
    return {
        "name": data["name"],
        "running": data["running"],
        "health": data["health"],
        "status": data["status"],
    }


def _demux_docker_logs(raw: bytes) -> str:
    """Docker's log stream frames each chunk with an 8-byte header when the
    container has no TTY. Strip the headers; fall back to raw decode if the
    stream isn't framed (TTY containers)."""
    out, i = [], 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4 : i + 8], "big")
        i += 8
        out.append(raw[i : i + size])
        i += size
    if not out:
        return raw.decode("utf-8", "replace")
    return b"".join(out).decode("utf-8", "replace")


@mcp.tool()
def container_logs(name: str, tail: int = 100) -> str:
    """Last `tail` lines of a container's logs (bounded)."""
    r = _client.get(
        f"{DOCKER_PROXY}/containers/{name}/logs",
        params={"stdout": "1", "stderr": "1", "tail": str(tail)},
    )
    r.raise_for_status()
    return _demux_docker_logs(r.content)


@mcp.tool()
def disk_health() -> dict:
    """Scrutiny SMART summary per disk."""
    return _get_json(f"{SCRUTINY}/api/summary")


@mcp.tool()
def cert_expiry(host: str, port: int = 443) -> dict:
    """TLS certificate expiry for host:port."""
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ss:
            cert = ss.getpeercert()
    not_after = cert.get("notAfter")
    expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
        tzinfo=timezone.utc
    )
    return {
        "host": host,
        "not_after": not_after,
        "days_remaining": (expires - datetime.now(timezone.utc)).days,
    }


@mcp.tool()
def ha_state(entity_id: str) -> dict:
    """Live Home Assistant state for one entity."""
    data = _get_json(
        f"{HA}/api/states/{entity_id}", headers={"Authorization": f"Bearer {HA_TOKEN}"}
    )
    return {
        "entity_id": data.get("entity_id"),
        "state": data.get("state"),
        "attributes": data.get("attributes", {}),
        "last_updated": data.get("last_updated"),
    }


@mcp.tool()
def host_overview() -> dict:
    """Host CPU/memory/disk/load/temperature at a glance."""
    queries = {
        "load1": "node_load1",
        "cpu_used_pct": '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
        "mem_used_pct": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
        "disk_root_used_pct": '100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})',
        "temp_celsius": "node_hwmon_temp_celsius",
    }
    return {
        k: safe_reads.parse_metric(
            _get_json(f"{PROMETHEUS}/api/v1/query", {"query": q})
        )
        for k, q in queries.items()
    }


@mcp.tool()
def top_containers(by: str = "cpu", n: int = 5) -> list[dict]:
    """Top-N containers by cpu or memory (cAdvisor)."""
    if by == "mem":
        q = f'topk({n}, container_memory_working_set_bytes{{name!=""}})'
    else:
        q = f'topk({n}, rate(container_cpu_usage_seconds_total{{name!=""}}[5m]))'
    return safe_reads.parse_metric(
        _get_json(f"{PROMETHEUS}/api/v1/query", {"query": q})
    )


@mcp.tool()
def read_file(path: str) -> str:
    """Read a text file under the ansible/ source tree (read-only, jailed).

    Rejects paths that escape the tree or name a secret/key file.
    """
    if safe_reads.path_is_denied(path):
        raise ValueError("access to that path is denied")
    resolved = safe_reads.resolve_within_jail(FILE_ROOT, path)
    if not resolved.is_file():
        raise ValueError("not a file")
    if resolved.stat().st_size > FILE_MAX_BYTES:
        raise ValueError(f"file exceeds {FILE_MAX_BYTES // 1024}KB read cap")
    return resolved.read_text(encoding="utf-8", errors="replace")


@mcp.tool()
def list_files(directory: str = ".") -> list[dict]:
    """List entries under the ansible/ source tree (jailed; secrets hidden)."""
    resolved = safe_reads.resolve_within_jail(FILE_ROOT, directory)
    if not resolved.is_dir():
        raise ValueError("not a directory")
    rel_base = directory.rstrip("/")
    entries = []
    for child in sorted(resolved.iterdir()):
        rel = f"{rel_base}/{child.name}" if rel_base not in ("", ".") else child.name
        if safe_reads.path_is_denied(rel):
            continue
        entries.append({"name": child.name, "is_dir": child.is_dir()})
    return entries


class _BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if not safe_reads.bearer_token_valid(
            request.headers.get("authorization"), TOKEN
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app():
    app = mcp.streamable_http_app()
    app.add_middleware(_BearerAuth)
    app.add_route("/health", lambda request: PlainTextResponse("ok"), methods=["GET"])
    return app


if __name__ == "__main__":
    import uvicorn

    if not TOKEN:
        raise SystemExit("HOMELAB_MCP_TOKEN is required")
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
