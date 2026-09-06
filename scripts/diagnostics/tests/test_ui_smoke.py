"""Live UI smoke: does a service's page actually render, behind a real login?

**Deselected by default.** These need things a GitHub runner does not have — the host's age
key (the fixture reads `domain` from SOPS), LAN reachability of the MetalLB ingress VIP, and
the Node `@playwright/mcp` install — so `addopts` carries `-m 'not ui'`. Everything imported
here is stdlib or pytest, so collection itself cannot fail on a machine with no browser.
`ansible/tests/leakguard.py` exempts the `ui` marker from its PATH shims, because the `domain`
fixture below decrypts SOPS before anything renders.

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
Those panels are the tier this module does NOT hold: `test_ui_smoke_grafana.py` logs into
Grafana and counts what a dashboard drew.
"""

import json
import re
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

REPO_ROOT = Path(__file__).resolve().parents[3]
WRAPPER = REPO_ROOT / "scripts" / "diagnostics" / "ui_mcp.sh"

# Hardcoded, NOT derived from containers_list: deriving it would silently enlist every new
# service into a suite CI never runs, so the next person to type `-m ui` inherits failures
# they did not cause. One service per node placement, plus two that carry their own login.
# (service, exact page title, path it should land on)
SERVICES = [
    ("homepage", "My Awesome Homepage", "/"),
    ("sonarr", "Sonarr", "/"),
    ("freshrss", "Login · FreshRSS", "/i/"),
    # Routed by the `claude-otel` role, whose containers_list entry names Grafana alone.
    # `Grafana` at `/login` is Grafana's OWN login page: it sits behind the one_factor
    # Authelia rule, so reaching it proves ingress → Authelia → backend. It does NOT prove a
    # dashboard renders — the 19 dead panels in this module's docstring would score green
    # here, because they are a logged-in page this tier never reaches.
    ("grafana", "Grafana", "/login"),
]

# `two_factor` in Authelia's access control, so a one_factor cookie is turned away at the
# portal (config-secret.yaml.j2:85-99). These skip unless a live two_factor session exists,
# which only `ui_login.py --totp <code>` can create — the shared secret stays on the phone,
# so there is no unattended path here by design. Titles observed through a real two_factor
# session; code-server carries its own login on top of Authelia, as FreshRSS does.
TWO_FACTOR_SERVICES = [
    ("longhorn", "Longhorn", "/#/dashboard"),
    ("code-server", "code-server login", "/login"),
    ("n8n", "n8n.io - Workflow Automation", "/"),
]

MINT_HINT = (
    "mint one with `uv run python scripts/diagnostics/ui_login.py --totp <code>`"
)


# Both budgets absorb a transient, not a slow load. Measured 2026-08-30: a settled title
# arrives on the second read, and a resultless evaluate succeeded on its retry every time.
_EVALUATE_ATTEMPTS = 3
_EVALUATE_RETRY_INTERVAL = 0.5
_TITLE_SETTLE_ATTEMPTS = 4
_TITLE_SETTLE_INTERVAL = 0.75


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

    def evaluate(self, js: str):
        """Run JS in the page and return its parsed result.

        **A reply with no result block is retried, not reported.** The server answers an
        evaluate with the code it ran plus a `### Result` section, and occasionally — seen
        on 2026-08-30, shortly after a navigation — it returns the echo alone. That is the
        transport having a moment, not the page saying anything, so treating it as a verdict
        fails a service that is fine.
        """
        last = ""
        for attempt in range(_EVALUATE_ATTEMPTS):
            if attempt:
                time.sleep(_EVALUATE_RETRY_INTERVAL)
            result = self.call(
                "tools/call",
                {"name": "browser_evaluate", "arguments": {"function": js}},
            )
            last = "\n".join(
                block.get("text", "")
                for block in result.get("content") or []
                if block.get("type") == "text"
            )
            # Only the "### Result" block: the rest is the echoed code.
            match = re.search(r"### Result\n(.*?)(?=\n### |\Z)", last, re.S)
            if match:
                value = json.loads(match.group(1).strip())
                return json.loads(value) if isinstance(value, str) else value
        raise AssertionError(
            f"browser_evaluate returned no result in {_EVALUATE_ATTEMPTS} attempts:\n"
            f"{last[:400]}"
        )

    def settled_title(self, expected: str) -> str:
        """The page title once it stops changing, or the last one seen.

        A single-page app can pass through a title of its own before applying its
        configured one — homepage momentarily reads `Homepage` before `My Awesome
        Homepage`, and the navigate report captures whichever moment it caught. Reading once
        therefore fails a service whose title is correct half a second later.
        """
        last = ""
        for attempt in range(_TITLE_SETTLE_ATTEMPTS):
            if attempt:
                time.sleep(_TITLE_SETTLE_INTERVAL)
            last = self.evaluate("() => JSON.stringify(document.title)")
            if last == expected:
                return last
        return last

    def close(self) -> None:
        """Shut the wrapper down and close every pipe it was given.

        All three, not just stdin: `filterwarnings = ["error"]` turns the ResourceWarning an
        unclosed `stdout`/`stderr` raises at GC into a teardown ERROR on the LAST test of the
        run, which reads as that dashboard having failed rather than as a leaked file object.
        """
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def http_status(report: str) -> int | None:
    """The status Playwright reported, or None when it reported none (a 200 is not always printed).

    Checked before the title so a mid-rollout 404 says `HTTP 404` rather than making someone work
    backwards from a missing title.
    """
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
    from diagnostics.probe_lib import core

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


def assert_serves_ui(
    report, is_error, domain, service, title, path, remint, observed_title=None
):
    """The four claims both tiers make, in the order that gives the best failure message.

    `observed_title` is the title read back after the page settled. It supersedes the one in
    the navigate report, which is whatever the title happened to be at load-complete — an
    app that sets its own title during hydration is still mid-change at that moment.
    """
    assert not is_error, f"{service}: navigation reported an error:\n{report}"
    assert "auth.local." not in report, (
        f"{service}: landed on the Authelia portal, so the session cookie was not accepted. "
        f"Re-mint it with `{remint}`."
    )
    status = http_status(report)
    assert status is None or status < 400, (
        f"{service}: the route answered HTTP {status}. A 404 here usually means the pod is "
        f"mid-rollout rather than that the route is wrong — check `probe.py health {service}`."
    )
    seen = page_title(report) if observed_title is None else observed_title
    assert seen == title, (
        f"{service}: expected the page titled {title!r}, got {seen!r}. "
        f"An absent title means Traefik answered rather than the app."
    )
    assert f"Page URL: https://{service}.local.{domain}{path}" in report, (
        f"{service}: expected to land on {path!r}; the app redirected somewhere else."
    )


@pytest.mark.parametrize(
    "service,title,path", SERVICES, ids=[s for s, _, _ in SERVICES]
)
def test_service_serves_its_own_ui(browser, domain, service, title, path):
    report, is_error = browser.navigate(f"https://{service}.local.{domain}/")
    assert_serves_ui(
        report,
        is_error,
        domain,
        service,
        title,
        path,
        remint="uv run python scripts/diagnostics/ui_login.py",
        observed_title=browser.settled_title(title),
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
def two_factor_browser(domain):
    """A browser carrying a two_factor session, or a skip when none is live.

    Skipping rather than failing is the honest outcome: a two_factor session lasts about an
    hour and can only be minted by a human typing a code, so its absence is the normal state
    of this machine, not a regression in anything.

    **The gate is a real browse, not `/api/state`.** It used to ask `ui_login.py --check
    --two-factor`, and the two questions came apart: measured 2026-08-29, the check reported
    the session live, the wrapper's own check agreed, and all three navigations then landed
    on the portal — three failures where three skips were the truth. Asking the browser to
    fetch a `two_factor` route is the same question the tests ask, so the guard and the
    assertions cannot disagree by construction.

    The `--check` call survives as a cheap pre-filter only, to avoid starting Chromium when
    the session is plainly gone.
    """
    # Through `uv run`, not the shebang: ui_login imports core, which uses PEP 758
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
        pytest.skip(f"no live two_factor session — {MINT_HINT}")

    client = McpClient([str(WRAPPER), "--two-factor"])
    try:
        try:
            client.call(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ui-smoke-2fa", "version": "1"},
                },
            )
        except AssertionError as exc:
            # The wrapper runs its own check and exits when the session is gone. That is the
            # same condition as the pre-filter above, so it deserves the same skip rather
            # than an error about a server that failed to start.
            if "two_factor session" not in str(exc):
                raise
            pytest.skip(f"the wrapper found no live two_factor session — {MINT_HINT}")
        client.notify("notifications/initialized")

        canary = TWO_FACTOR_SERVICES[0][0]
        report, _ = client.navigate(f"https://{canary}.local.{domain}/")
        if "auth.local." in report:
            pytest.skip(
                f"the two_factor session no longer opens {canary} — {MINT_HINT}"
            )

        yield client
    finally:
        client.close()


@pytest.mark.parametrize(
    "service,title,path",
    TWO_FACTOR_SERVICES,
    ids=[s for s, _, _ in TWO_FACTOR_SERVICES],
)
def test_two_factor_service_serves_its_own_ui(
    two_factor_browser, domain, service, title, path
):
    """Same four claims as the one_factor tier, plus the fact that the second factor worked.

    Reaching any of these at all requires `authentication_level 2`.
    """
    report, is_error = two_factor_browser.navigate(f"https://{service}.local.{domain}/")
    assert_serves_ui(
        report,
        is_error,
        domain,
        service,
        title,
        path,
        # A two_factor session lapses after about an hour, so this is the usual reason.
        remint="uv run python scripts/diagnostics/ui_login.py --totp <code>",
        observed_title=two_factor_browser.settled_title(title),
    )
