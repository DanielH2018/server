"""vault-mcp: read-only MCP server over the Obsidian vault livesync-cli mirrors into /vault.

Streamable-HTTP MCP server. Every tool is a read. The vault claim is mounted read-only, so no
code path here can write a note. Traefik's router already gates requests on the bearer token,
and the app re-checks it. The hide rule and every read live in vault_notes.py; this file is
the wiring.

Usage:

    python app.py

Takes no arguments. The image runs it as the container command, and it serves MCP over HTTP
until killed. `-h` and `--help` print this text and exit 0.

Environment:

    VAULT_MCP_TOKEN          bearer token every request must carry (required; exits without it)
    VAULT_MCP_ROOT           the vault directory (default /vault)
    VAULT_MCP_EXCLUDE_PATHS  newline-separated folders hidden from every tool, beside the
                             hidden-segment rule vault_notes.py builds in
    MCP_PUBLIC_HOST          external hostname Traefik forwards, allowlisted for the transport
    PORT                     TCP port to listen on (default 8000)
"""

# pyright: reportMissingImports=false

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print((__doc__ or "").strip())
        sys.exit(0)

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse

import safe_reads
import vault_notes

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

TOKEN = os.environ.get("VAULT_MCP_TOKEN", "")
PUBLIC_HOST = os.environ.get("MCP_PUBLIC_HOST", "")
VAULT = vault_notes.Vault(
    Path(os.environ.get("VAULT_MCP_ROOT", "/vault")),
    os.environ.get("VAULT_MCP_EXCLUDE_PATHS", "").splitlines(),
)

_hosts, _origins = safe_reads.allowed_hosts_and_origins(PUBLIC_HOST)
mcp = FastMCP(
    "vault",
    transport_security=(
        TransportSecuritySettings(allowed_hosts=_hosts, allowed_origins=_origins)
        if PUBLIC_HOST
        else None
    ),
)


@mcp.tool()
def search(
    query: str = "", tag: str = "", path: str = "", limit: int = 20
) -> list[dict]:
    """Full-text search of the vault's notes; every word of `query` must appear.

    `tag` keeps notes with that tag (or a nested child tag), `path` keeps notes under that
    folder. Returns each match's path, title and a snippet, best match first.
    """
    return VAULT.search(query, tag=tag, path=path, limit=limit)


@mcp.tool()
def read_note(path: str) -> dict:
    """One note's body, frontmatter, tags and outgoing links, by its vault-relative path."""
    return VAULT.read_note(path)


@mcp.tool()
def list_folder(path: str = "") -> list[dict]:
    """The folders, notes and other files directly inside a vault folder (root by default)."""
    return VAULT.list_folder(path)


@mcp.tool()
def recent_notes(limit: int = 20) -> list[dict]:
    """The most recently modified notes, newest first."""
    return VAULT.recent_notes(limit)


@mcp.tool()
def backlinks(path: str) -> list[dict]:
    """The notes that link to the note at `path`."""
    return VAULT.backlinks(path)


class _BearerAuth(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path == "/health":
            return await call_next(request)
        if not safe_reads.bearer_token_valid(
            request.headers.get("authorization"), TOKEN
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app() -> Starlette:
    app = mcp.streamable_http_app()
    app.add_middleware(_BearerAuth)
    app.add_route("/health", lambda request: PlainTextResponse("ok"), methods=["GET"])
    return app


if __name__ == "__main__":
    import uvicorn

    if not TOKEN:
        raise SystemExit("VAULT_MCP_TOKEN is required")
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
