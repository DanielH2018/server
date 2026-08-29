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

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "diagnostics" / "ui_mcp.sh"

# Hardcoded, NOT derived from containers_list: deriving it would silently enlist every new
# service into a suite CI never runs, so the next person to type `-m ui` inherits failures
# they did not cause. One service per node placement, plus one that carries its own login.
# (service, exact page title, path it should land on)
SERVICES = [
    ("homepage", "My Awesome Homepage", "/"),
    ("sonarr", "Sonarr", "/"),
    ("freshrss", "Login · FreshRSS", "/i/"),
]

# `two_factor` in Authelia's access control, so a one_factor cookie is turned away at the
# portal (config-secret.yaml.j2:85-99). These skip unless a live two_factor session exists,
# which only `ui_login.py --totp <code>` can create — the shared secret stays on the phone,
# so there is no unattended path here by design.
TWO_FACTOR_SERVICES = ["longhorn", "code-server", "n8n"]


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


def http_status(report: str) -> int | None:
    """The status Playwright reported, or None when it reported none (a 200 is not always
    printed). Checked before the title so a mid-rollout 404 says `HTTP 404` rather than
    making someone work backwards from a missing title."""
    for line in report.splitlines():
        if "HTTP status:" in line:
            return int(line.split("HTTP status:", 1)[1].strip())
    return None


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
    status = http_status(report)
    assert status is None or status < 400, (
        f"{service}: the route answered HTTP {status}. A 404 here usually means the pod is "
        f"mid-rollout rather than that the route is wrong — check `probe.py health {service}`."
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


@pytest.fixture(scope="module")
def two_factor_browser():
    """A browser carrying a two_factor session, or a skip when none is live.

    Skipping rather than failing is the honest outcome: a two_factor session lasts about an
    hour and can only be minted by a human typing a code, so its absence is the normal state
    of this machine, not a regression in anything.
    """
    # Through `uv run`, not the shebang: ui_login imports probe_core, which uses PEP 758
    # syntax that Ubuntu's 3.12 /usr/bin/python3 cannot parse.
    proc = subprocess.run(
        [
            "uv",
            "run",
            "--directory",
            str(REPO_ROOT),
            "python",
            "scripts/diagnostics/ui_login.py",
            "--check",
            "--two-factor",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(
            "no live two_factor session — mint one with "
            "`uv run python scripts/diagnostics/ui_login.py --totp <code>`"
        )
    client = McpClient([str(WRAPPER), "--two-factor"])
    try:
        client.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ui-smoke-2fa", "version": "1"},
            },
        )
        client.notify("notifications/initialized")
        yield client
    finally:
        client.close()


@pytest.mark.parametrize("service", TWO_FACTOR_SERVICES)
def test_two_factor_service_is_reachable(two_factor_browser, domain, service):
    """That the two_factor session gets past the portal and the app answers.

    Deliberately weaker than the one_factor tests above, which pin an exact title. These
    three cannot be reached to learn their titles without a live code, and inventing one
    would be a guess dressed as an assertion. Not-the-portal plus a title present is what is
    actually being claimed: the second factor worked and the backend served HTML.
    """
    report, is_error = two_factor_browser.navigate(f"https://{service}.local.{domain}/")

    assert not is_error, f"{service}: navigation reported an error:\n{report}"
    assert "auth.local." not in report, (
        f"{service}: landed on the Authelia portal, so the two_factor session was not "
        f"accepted. Re-mint with `ui_login.py --totp <code>`; it lapses after about an hour."
    )
    status = http_status(report)
    assert status is None or status < 400, (
        f"{service}: the route answered HTTP {status} — check `probe.py health {service}`."
    )
    assert page_title(report) is not None, (
        f"{service}: no page title, so Traefik answered rather than the app."
    )
