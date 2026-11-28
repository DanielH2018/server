"""The internet-facing gate: which requests vault_server.py's real ASGI app answers without a token.

The requests are raw ASGI scopes rather than an HTTP client's, because a client normalises
`/health/../mcp` and `//mcp` before they leave it, and uvicorn hands the app the path as sent.
"""

import asyncio
import importlib
import sys

import pytest

RESOURCE = "https://vault-mcp.example.com/mcp"
ISSUER = "https://auth.example.com"
METADATA = "/.well-known/oauth-protected-resource"
BARE_CHALLENGE = (
    'Bearer resource_metadata="https://vault-mcp.example.com'
    '/.well-known/oauth-protected-resource/mcp"'
)


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    with pytest.MonkeyPatch.context() as env:
        env.setenv("VAULT_MCP_RESOURCE_URL", RESOURCE)
        env.setenv("VAULT_MCP_ISSUER", ISSUER)
        env.setenv("VAULT_MCP_JWKS_URL", "https://auth.local.example.com/jwks.json")
        env.setenv("VAULT_MCP_ROOT", str(tmp_path_factory.mktemp("vault")))
        env.setenv("VAULT_MCP_EXCLUDE_PATHS", "Private")
        env.delenv("MCP_HOSTS", raising=False)
        sys.modules.pop("vault_server", None)
        yield importlib.import_module("vault_server").build_app()
    sys.modules.pop("vault_server", None)


def _request(app, method, path):
    """The status and headers `app` answers a request for `path` with, no credentials sent."""
    messages = []
    body_sent = False

    async def receive():
        nonlocal body_sent
        if body_sent:
            return {"type": "http.disconnect"}
        body_sent = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"vault-mcp.example.com")],
        "client": ("203.0.113.9", 40000),
        "server": ("vault-mcp.example.com", 443),
    }
    asyncio.run(app(scope, receive, send))
    (start,) = [m for m in messages if m["type"] == "http.response.start"]
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}


@pytest.mark.parametrize("path", ["/health", f"{METADATA}/mcp", METADATA])
def test_health_and_the_metadata_answer_a_get_without_a_token(app, path):
    status, _ = _request(app, "GET", path)
    assert status == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/mcp"),
        ("GET", "/mcp"),
        ("POST", "/mcp/"),
        ("POST", "//mcp"),
        ("GET", "/HEALTH"),
        ("GET", "/health/"),
        ("GET", "/health/../mcp"),
        ("POST", "/health"),
        ("POST", METADATA),
    ],
)
def test_every_other_request_without_a_token_gets_the_challenge(app, method, path):
    status, headers = _request(app, method, path)
    assert (status, headers.get("www-authenticate")) == (401, BARE_CHALLENGE)
