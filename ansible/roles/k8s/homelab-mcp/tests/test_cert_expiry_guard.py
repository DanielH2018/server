"""cert_expiry refuses a target outside the routed zone BEFORE any socket is opened (#1931).

The predicate lives in safe_reads and has its own table; this file proves the wiring in
app.py calls it first. app.py imports mcp, httpx and starlette, none of which the test env
carries, so those are stubbed in sys.modules for the import: the tool decorator returns the
function unchanged, which is all `cert_expiry` needs to be called directly.

The image carries `obs_api` flat beside app.py. Here a copy of the repo module under
`scripts/diagnostics/probe_lib` takes the image's bare name, with `get_json` replaced by a
recorder, so the last test can prove a Loki read always sends an explicit window.

Run: uv run pytest ansible/roles/k8s/homelab-mcp/tests/test_cert_expiry_guard.py
"""

import importlib
import os
import socket
import sys
import types

import pytest

from diagnostics.probe_lib import obs_api

FILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files")
sys.path.insert(0, FILES)


class _FastMCP:
    def __init__(self, *_a, **_k):
        pass

    def tool(self, *_a, **_k):
        return lambda fn: fn


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setitem(sys.modules, "mcp", _stub("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", _stub("mcp.server"))
    monkeypatch.setitem(
        sys.modules, "mcp.server.fastmcp", _stub("mcp.server.fastmcp", FastMCP=_FastMCP)
    )
    monkeypatch.setitem(
        sys.modules,
        "mcp.server.transport_security",
        _stub("mcp.server.transport_security", TransportSecuritySettings=object),
    )
    monkeypatch.setitem(
        sys.modules,
        "httpx",
        _stub("httpx", Client=lambda *a, **k: object(), Timeout=lambda *a, **k: None),
    )
    recorder = _stub("obs_api", **vars(obs_api))
    recorder.seen = []
    recorder.get_json = lambda url: recorder.seen.append(url) or {}
    monkeypatch.setitem(sys.modules, "obs_api", recorder)
    monkeypatch.setitem(sys.modules, "starlette", _stub("starlette"))
    monkeypatch.setitem(
        sys.modules, "starlette.middleware", _stub("starlette.middleware")
    )
    monkeypatch.setitem(
        sys.modules,
        "starlette.middleware.base",
        _stub("starlette.middleware.base", BaseHTTPMiddleware=object),
    )
    monkeypatch.setitem(
        sys.modules,
        "starlette.responses",
        _stub("starlette.responses", JSONResponse=object, PlainTextResponse=object),
    )
    monkeypatch.setenv("HOMELAB_MCP_TOKEN", "t" * 32)
    monkeypatch.setenv("CERT_EXPIRY_DOMAINS", "example.com")
    monkeypatch.delenv("CERT_EXPIRY_PORTS", raising=False)
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    yield module
    sys.modules.pop("app", None)


@pytest.fixture
def no_socket(monkeypatch):
    """Fail the test outright if anything tries to connect."""

    def refuse(*_a, **_k):
        raise AssertionError("socket.create_connection was called")

    monkeypatch.setattr(socket, "create_connection", refuse)


def test_a_closed_loopback_port_is_refused_before_any_socket_opens(app, no_socket):
    with pytest.raises(ValueError, match="not a TLS endpoint this homelab routes"):
        app.cert_expiry("127.0.0.1", 1)


@pytest.mark.parametrize(
    "host,port",
    [
        ("n8n", 5678),
        ("10.42.0.7", 443),
        ("evil-example.com", 443),
        ("n8n.example.com", 80),
    ],
)
def test_off_zone_targets_are_refused_before_any_socket_opens(
    app, no_socket, host, port
):
    with pytest.raises(ValueError):
        app.cert_expiry(host, port)


def test_an_in_zone_target_reaches_the_socket(app, monkeypatch):
    """The clean half: a routed hostname on 443 gets past the guard to the connect."""
    calls = []

    def record(addr, timeout=None):
        calls.append(addr)
        raise ConnectionRefusedError("test stops here")

    monkeypatch.setattr(socket, "create_connection", record)
    with pytest.raises(ConnectionRefusedError):
        app.cert_expiry("n8n.example.com", 443)
    assert calls == [("n8n.example.com", 443)]


def test_a_loki_read_sends_an_explicit_backward_window(app):
    """Loki answers an unbounded query_range from its last hour only, so older rows vanish."""
    app.query_logs('{job="x"}', limit=5, hours=2.0)
    (url,) = app.obs_api.seen
    assert url.startswith(f"{app.LOKI}/loki/api/v1/query_range?")
    assert "start=" in url and "end=" in url and "direction=backward" in url
