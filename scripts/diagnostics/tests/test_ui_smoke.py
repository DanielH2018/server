"""Live UI smoke: does a service's page actually render, behind a real login?

**Deselected by default.** These need things a GitHub runner does not have — the host's age
key (the fixture reads `domain` from SOPS), LAN reachability of the MetalLB ingress VIP, and
the Node `@playwright/mcp` install — so `addopts` carries `-m 'not ui'`. Everything imported
here is stdlib, pytest, or the stdlib-only `grafana_panel_report`, so collection itself
cannot fail on a machine with no browser.

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
import re
import subprocess
import time
from pathlib import Path

import pytest
from grafana_panel_report import classify

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


# ---------------------------------------------------------------------------------------
# Third tier: past Grafana's own login, asserting on the panels a dashboard actually drew.
#
# The one_factor tier above stops at Grafana's login page, which proves ingress -> Authelia
# -> backend and nothing about a dashboard. That gap is not incidental: the 19 dead Angular
# panels in this module's docstring are exactly what a login-page check cannot see. This
# tier logs in and counts rendered panels.
#
# `admin` + the SOPS `grafana_admin_password`, POSTed to Grafana's own `/login` from inside
# the page. Same origin, so the Authelia cookie `ui_mcp.sh` already minted rides along and no
# route has to be opened up for a machine caller. **The password never leaves this process
# and the MCP server**: it is inlined into the evaluated JS, which the server echoes back in
# its reply, so every path that could surface that reply scrubs it first (`_scrub`).
# ---------------------------------------------------------------------------------------

# Hardcoded for the reason SERVICES is: deriving this from files/dashboards/ would enlist
# every new board into a suite CI never runs. One per rendering shape, and `min_headers` is
# the count observed live on 2026-08-30 — a deliberate dashboard change updates the number.
# (uid, minimum rendered panel headers)
GRAFANA_DASHBOARDS = [
    ("longhorn-storage", 13),
    ("claude-code-otel", 25),
    ("ddmlqvk12uozka", 18),  # traefik-custom
    # 4 panels, every one a `row`: it mounts fully and draws no panel header until a row is
    # expanded, so 0 here means "assert on rows instead". See grafana_panel_report.py.
    ("6L2GdB47z", 0),  # crowdsec-details-per-machine
]

# Measured 2026-08-30: a dashboard that mounts draws its panels within ~2.2s, and 15 of 19
# did so on the first sample. Samples beyond that never changed a verdict, so the budget is
# for a slow cycle, not for a settle that keeps moving.
_PANEL_SAMPLES = 6
_PANEL_SAMPLE_INTERVAL = 1.5
# The un-mounted signature is a client-side race, not a slow load — it never resolves by
# waiting (16s of samples, unchanged), only by navigating again.
_MOUNT_ATTEMPTS = 3

_PANEL_STATE_JS = """() => {
    const hdr = '[data-testid^="data-testid Panel header "]';
    return JSON.stringify({
      ready: document.readyState,
      path: location.pathname,
      testids: document.querySelectorAll('[data-testid]').length,
      headers: document.querySelectorAll(hdr).length,
      rows: document.querySelectorAll('[data-testid^="data-testid dashboard-row"]').length,
      statusError: document.querySelectorAll(
        '[data-testid="data-testid Panel status error"]').length,
      errorTexts: [...document.querySelectorAll(
        '[data-testid="data-testid Panel data error message"]')]
        .map(e => e.innerText.trim().slice(0, 120)),
      pluginNotFound: document.body.innerText.includes('Panel plugin not found'),
    });
}"""


class GrafanaPage:
    """A logged-in Grafana, wrapping the MCP client with the two things this tier needs.

    An evaluate that returns parsed JSON, and a navigate that survives the un-mounted race.
    """

    def __init__(self, client: McpClient, base: str, secret: str) -> None:
        self.client = client
        self.base = base
        self._secret = secret

    def _scrub(self, text: str) -> str:
        return text.replace(self._secret, "<redacted>")

    def evaluate(self, js: str):
        """The client's evaluate, with the password kept out of anything it raises.

        The server echoes the JS it ran, and this tier's login inlines the credential into
        that JS, so every failure message from here has to be scrubbed before it surfaces.
        """
        try:
            return self.client.evaluate(js)
        except AssertionError as exc:
            raise AssertionError(self._scrub(str(exc))) from None

    def _settle_on_grafana(self) -> str:
        """Navigate to Grafana and return once the page really is Grafana.

        `browser_navigate` returns before the document is necessarily the one asked for, and
        a `fetch('/login')` evaluated a moment too early POSTs to whatever origin the browser
        is still on — which answers 404 and reads exactly like a wrong password.
        """
        seen = "nothing"
        for _ in range(_MOUNT_ATTEMPTS):
            # Via about:blank, for the reason open_dashboard does it, and because a stale
            # coalesced connection is what produces the 421 the fetches below retry past.
            self.client.navigate("about:blank")
            self.client.navigate(self.base + "/")
            for _ in range(_PANEL_SAMPLES):
                time.sleep(_PANEL_SAMPLE_INTERVAL)
                where = self.evaluate(
                    "() => JSON.stringify({host: location.host,"
                    " ready: document.readyState})"
                )
                seen = f"{where['host']} ({where['ready']})"
                if (
                    where["host"].startswith("grafana.")
                    and where["ready"] == "complete"
                ):
                    return seen
        raise AssertionError(
            f"never landed on Grafana to log in — the browser sat on {seen}. Landing on "
            f"auth.local.* means the Authelia session lapsed; re-mint it with "
            f"`uv run python scripts/diagnostics/ui_login.py`."
        )

    # Every same-origin fetch this tier makes goes through this: `ui_mcp.sh` pins the whole
    # `*.local.<domain>` space to one ingress VIP behind one certificate, so Chromium
    # coalesces HTTP/2 connections across those hostnames and Traefik answers a request that
    # arrives on another host's connection with 421 Misdirected Request. The browser drops
    # the offending connection, so a retry lands on a fresh one.
    _FETCH = """async (url, opt) => {
      for (let i = 0; i < 5; i++) {
        const r = await fetch(url, Object.assign({cache: 'no-store'}, opt));
        if (r.status !== 421) return r;
        await new Promise(done => setTimeout(done, 400));
      }
      return null;
    }"""

    def _signed_in_as(self):
        """The logged-in Grafana user's login name, or None when the session is anonymous."""
        return self.evaluate(
            "async () => { const fetchOk = %s;"
            " const r = await fetchOk('/api/user', {});"
            " if (!r || r.status !== 200) return JSON.stringify(null);"
            " return JSON.stringify((await r.json()).login); }" % self._FETCH
        )

    def login(self) -> str:
        """Make sure the browser holds a Grafana session, and return whose it is.

        **Checked before it is minted, not after.** Grafana registers `POST /login` behind
        its not-signed-in middleware, so the endpoint that mints a session answers 404 once
        one exists — and `ui_mcp.sh`'s browser profile carries the previous run's session.
        Posting unconditionally therefore fails on every run after the first, with a 404 that
        reads like a wrong password.
        """
        self._settle_on_grafana()
        who = self._signed_in_as()
        if who:
            return who
        status = self.evaluate(
            "async () => { const fetchOk = %s;"
            " const r = await fetchOk('/login', {method: 'POST',"
            " headers: {'content-type': 'application/json'},"
            " body: JSON.stringify({user: 'admin', password: %s})});"
            " return r ? r.status : 421; }" % (self._FETCH, json.dumps(self._secret))
        )
        assert status == 200, (
            f"Grafana rejected the admin login with HTTP {status}. The SOPS "
            f"`grafana_admin_password` and the live `grafana-admin` Secret have diverged — "
            f"`kubectl apply` leaves stale Secret keys, so a rotation that was never applied "
            f"looks exactly like this."
        )
        who = self._signed_in_as()
        assert who, "Grafana accepted the login but /api/user still reports no session"
        return who

    def open_dashboard(self, uid: str, min_headers: int):
        """Navigate and sample until the page yields a verdict that is not a retry.

        `about:blank` first: navigating straight from one Grafana URL to another leaves the
        SPA on the old view, which reads as a dashboard that rendered someone else's panels.
        """
        last = None
        for _ in range(_MOUNT_ATTEMPTS):
            self.client.navigate("about:blank")
            self.client.navigate(f"{self.base}/d/{uid}/?from=now-6h&to=now")
            for _ in range(_PANEL_SAMPLES):
                time.sleep(_PANEL_SAMPLE_INTERVAL)
                last = classify(self.evaluate(_PANEL_STATE_JS), min_headers=min_headers)
                # Only a pass ends the sampling. A page part-way through loading looks
                # exactly like a broken one — panels missing, a query still in flight — so
                # returning the first non-pass would report whatever the load happened to
                # look like at 1.5s. Keep sampling and let the settled state be the verdict.
                if last.ok:
                    return last
            if not last.worth_renavigating:
                return last
        return last


@pytest.fixture(scope="module")
def grafana(domain):
    """A Grafana logged in as admin, or a skip when the credential is not readable.

    Module-scoped for the same reason `browser` is: one Chromium and one login for the
    whole tier.
    """
    import probe_core as core

    try:
        password = core.sops_extract("grafana_admin_password")
    except (
        Exception
    ) as exc:  # a host with no age key, which is not a Grafana regression
        pytest.skip(f"cannot read grafana_admin_password from SOPS: {exc}")

    client = McpClient([str(WRAPPER)])
    try:
        client.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ui-smoke-grafana", "version": "1"},
            },
        )
        client.notify("notifications/initialized")
        page = GrafanaPage(client, f"https://grafana.local.{domain}", password)
        assert page.login() == "admin", (
            "logged into Grafana as someone other than admin"
        )
        yield page
    finally:
        client.close()


@pytest.mark.parametrize(
    "uid,min_headers", GRAFANA_DASHBOARDS, ids=[u for u, _ in GRAFANA_DASHBOARDS]
)
def test_grafana_dashboard_renders_its_panels(grafana, uid, min_headers):
    """The claim the one_factor tier cannot make: this dashboard drew its panels.

    Readiness, the pod, the startup log and the `validate-grafana-dashboards` hook all read
    green through the 2026-08-22 incident, because Grafana serves a dashboard whose panel
    type it no longer implements and the frontend simply draws nothing.
    """
    verdict = grafana.open_dashboard(uid, min_headers)
    assert verdict is not None, f"{uid}: no sample was taken at all"
    assert not verdict.retryable, (
        f"{uid}: the Grafana app never mounted across {_MOUNT_ATTEMPTS} navigations — "
        f"{verdict.detail}. That is normally the harness race, so a persistent one is "
        f"itself the finding."
    )
    assert verdict.ok, f"{uid}: {verdict.detail}"
