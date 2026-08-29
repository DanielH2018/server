"""Live UI smoke: does a service's page actually render, behind a real login?

**Deselected by default.** These need things a GitHub runner does not have — the host's age
key (the fixture reads `domain` from SOPS), LAN reachability of the MetalLB ingress VIP, and
the Node `@playwright/mcp` install — so `addopts` carries `-m 'not ui'`. Everything imported
here is stdlib plus pytest, so collection itself cannot fail on a machine with no browser.

    uv run pytest -m ui                      # run them
    uv run pytest -m ui -k homepage          # one service

They drive `ui_mcp.sh` over its MCP stdio interface rather than binding a browser in Python.
That keeps the repo to ONE Chromium — the Node one `@playwright/mcp` already installs — and
it exercises the surface Claude actually uses, so a break in the wrapper's DNS pin, its
session minting, or its launch config fails here rather than silently degrading a session.

**What these assert, precisely.** That the route serves the app's own HTML: ingress →
Authelia → backend → a page with the expected title at the expected path. That is strictly
weaker than "the logged-in UI works", because several services carry their OWN login behind
Authelia — FreshRSS lands on `/i/`, uptime-kuma on `/dashboard`, karakeep on `/signin`. So
the expectations below pin the EXACT title rather than a substring of the service name:
`FreshRSS` alone also matches `Login · FreshRSS`, which is how a broken app scores green.

What this catches that `probe.py health <svc>` cannot: readiness flips a Deployment to
Available before anything renders, which is how 19 dead Grafana panels sat behind a 1/1 pod.
"""

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "diagnostics" / "ui_mcp.sh"

# Hardcoded, NOT derived from containers_list: deriving it would silently enlist every new
# service into a suite CI never runs, so the next person to type `-m ui` inherits failures
# they did not cause. One service per node placement, plus one that carries its own login.
# (service, exact page title, path it should land on)
SERVICES = [
    ("homepage", "My Awesome Homepage", "/"),
    ("sonarr", "Sonarr", "/"),
    ("freshrss", "Login · FreshRSS", "/i/"),
]


class McpClient:
    """A minimal JSON-RPC-over-stdio MCP client — enough to navigate and read the page."""

    def __init__(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._id = 0

    def _write(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method})

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        want = self._id
        self._write(
            {"jsonrpc": "2.0", "id": want, "method": method, "params": params or {}}
        )
        # The server interleaves notifications with replies on one stream, so read until the
        # id matches rather than trusting the next line to be ours.
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError(
                    f"{WRAPPER.name} exited during {method}: {self.proc.stderr.read().strip()}"
                )
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want:
                if "error" in msg:
                    raise AssertionError(f"{method} failed: {msg['error']}")
                return msg["result"]

    def navigate(self, url: str) -> tuple[str, bool]:
        """Go to a URL; return the server's text report and whether it flagged an error."""
        result = self.call(
            "tools/call", {"name": "browser_navigate", "arguments": {"url": url}}
        )
        text = "\n".join(
            block.get("text", "")
            for block in result.get("content") or []
            if block.get("type") == "text"
        )
        return text, bool(result.get("isError"))

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def page_title(report: str) -> str | None:
    """The title Playwright reported, or None when the page carried none — which is what
    Traefik's 404 looks like, and the reason a missing title must never pass."""
    for line in report.splitlines():
        if "Page Title:" in line:
            return line.split("Page Title:", 1)[1].strip()
    return None


@pytest.fixture(scope="module")
def domain() -> str:
    import probe_core as core

    return core.sops_extract("domain")


@pytest.fixture(scope="module")
def browser():
    """One browser for the whole module.

    Module-scoped on purpose: `ui_mcp.sh` re-validates the Authelia session on every launch,
    so a function-scoped fixture would hit the first-factor endpoint once per service and
    pay a Chromium start each time.
    """
    client = McpClient([str(WRAPPER)])
    try:
        client.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ui-smoke", "version": "1"},
            },
        )
        client.notify("notifications/initialized")
        yield client
    finally:
        # Unconditional: a failed handshake must not strand a Chromium.
        client.close()


@pytest.mark.parametrize(
    "service,title,path", SERVICES, ids=[s for s, _, _ in SERVICES]
)
def test_service_serves_its_own_ui(browser, domain, service, title, path):
    report, is_error = browser.navigate(f"https://{service}.local.{domain}/")

    assert not is_error, f"{service}: navigation reported an error:\n{report}"
    assert "auth.local." not in report, (
        f"{service}: landed on the Authelia portal, so the session cookie was not accepted. "
        f"Re-mint it with `uv run python scripts/diagnostics/ui_login.py`."
    )
    assert page_title(report) == title, (
        f"{service}: expected the page titled {title!r}, got {page_title(report)!r}. "
        f"An absent title means Traefik answered rather than the app."
    )
    assert f"Page URL: https://{service}.local.{domain}{path}" in report, (
        f"{service}: expected to land on {path!r}; the app redirected somewhere else."
    )


def test_a_route_with_no_backend_is_not_scored_as_a_rendered_ui(browser, domain):
    """The rejecting half, and the reason the assertions above check a title at all.

    An unrouted host still resolves — the wrapper's resolver rule maps the whole
    `*.local.<domain>` space to the ingress VIP — so Traefik answers with a 404 carrying no
    title. Navigation therefore 'succeeds', and a suite that only checked for an error would
    score this as a healthy service.
    """
    report, is_error = browser.navigate(f"https://no-such-service.local.{domain}/")
    assert not is_error, "expected Traefik to answer rather than the navigation to fail"
    assert page_title(report) is None, (
        f"expected no title from an unrouted host, got {page_title(report)!r}"
    )
