"""Live Grafana smoke: does a dashboard actually draw its panels, past Grafana's own login?

**Split out of `test_ui_smoke.py`** rather than written apart from it: that module sat at the
module-length ratchet's ceiling, and this tier is the self-contained half. It shares that
module's `McpClient` and wrapper path by importing them, exactly as `test_ui_smoke_helpers.py`
already does.

The tiers in `test_ui_smoke.py` stop at Grafana's login page, which proves ingress -> Authelia
-> backend and nothing about a dashboard. That gap is not incidental: 19 dead Angular panels
sat behind a 1/1 pod for 55 minutes, and a login-page check cannot see them. This tier logs in
and counts rendered panels.

`admin` + the SOPS `grafana_admin_password`, POSTed to Grafana's own `/login` from inside the
page. Same origin, so the Authelia cookie `ui_mcp.sh` already minted rides along and no route
has to be opened up for a machine caller. **The password never leaves this process and the MCP
server**: it is inlined into the evaluated JS, which the server echoes back in its reply, so
every path that could surface that reply scrubs it first (`_scrub`).

    uv run pytest -m ui -k grafana

That command is also how a Claude session verifies a Grafana board it changed — driving the
browser by hand lands on `/login`, and the only way past it there is typing the admin password
into a tool call, which puts a live credential in the session transcript. See
`docs/claude-tooling.md` -> *The Grafana panel tier*.
"""

import json
import os
import sys
import time

import pytest

# `scripts/diagnostics` is deliberately absent from `pythonpath` in pyproject.toml, so this
# module puts its own parent directory on `sys.path` — the insert every sibling here carries.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grafana_panel_report import classify
from test_ui_smoke import WRAPPER, McpClient

pytestmark = pytest.mark.ui


# Duplicated rather than shared: a fixture does not cross modules, and a `conftest.py` here
# would apply to all 23 test modules in this directory to serve two of them.
@pytest.fixture(scope="module")
def domain() -> str:
    from diagnostics.probe_lib import core

    return core.sops_extract("domain")


# Hardcoded for the reason `test_ui_smoke.SERVICES` is: deriving it from files/dashboards/
# would enlist every new board into a suite CI never runs. One per rendering shape, and
# `min_headers` is
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
    from diagnostics.probe_lib import core

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
