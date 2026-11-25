#!/usr/bin/env python3
"""Classify what a Grafana dashboard page actually rendered.

Pure functions over the payload `test_ui_smoke.py`'s browser evaluate returns, kept out of
that module so they can be tested WITHOUT a browser — the `ui` marker is deselected in CI,
so a classifier living only inside a `ui` test would never run there at all.

**Why a classifier and not a bare assertion.** A zero-panel read has two causes that look
identical from an assertion and must not be treated alike:

  * The React app never mounted. `document.readyState` is `complete`, the URL is still the
    bare `/d/<uid>/` with no slug appended, the whole document carries ZERO `data-testid`
    attributes and `document.body.innerText` is the 19 characters of "Skip to main content".
    This is a client-side race in the harness, not a broken dashboard: it hit 5 dashboards in
    one run and a disjoint 2 in another, and every one of them rendered fully on a re-navigate.
    Retry it — never report it.
  * The dashboard is row-only. `crowdsec-details-per-machine` (4 panels, all `type: row`) and
    `crowdsec-insight` (1 row) mount correctly — hundreds of testids, a slug in the URL, row
    elements present — and draw no panel header until a row is expanded. That is the
    dashboard's real shape, so it is a pass with `rows`, not a failure with `headers`.

Anything else with no panels is the failure this is for: the dashboard mounted and drew
nothing. That is the shape of provisioning Angular `graph` panels to a Grafana that has dropped
Angular: nothing renders while the pod reads 1/1 with zero errors in the log.
"""

import re
from dataclasses import dataclass

# A bare `/d/<uid>/` URL means the dashboard never loaded; testid count alone cannot tell, as chrome
# alone is 27 to 53.
_MOUNTED_MIN_TESTIDS = 10
_BARE_DASHBOARD_URL = re.compile(r"^/d/[^/]+/?$")

# An empty panel is not a broken one: failing on 'No data' would be flaky.
# A panel whose metric died reads the same and is not caught here.
_BENIGN_PANEL_MESSAGES = {"no data"}


def _is_bare_dashboard_url(path: str) -> bool:
    return bool(_BARE_DASHBOARD_URL.match(path))


NOT_MOUNTED = "not_mounted"
OK = "ok"
FLAGGED = "flagged"


@dataclass(frozen=True)
class Verdict:
    """The result of classifying one dashboard's rendered panel state.

    Attributes:
        status: One of ``NOT_MOUNTED``, ``OK`` or ``FLAGGED``.
        detail: Human-readable explanation of the status.
        empty: Whether the dashboard mounted but drew no panel or row at all.
    """

    status: str
    detail: str
    empty: bool = False

    @property
    def retryable(self) -> bool:
        return self.status == NOT_MOUNTED

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def worth_renavigating(self) -> bool:
        """Whether to load the page again before believing this.

        A page that mounted and then drew NOTHING — no panel, no row — is the one verdict
        worth a second load. `crowdsec-details-per-machine` draws its 12 rows in 2.1s on an
        isolated load, and drew nothing as the fourth dashboard of a run in the same browser.
        Re-navigating cannot hide a real break, because a dashboard that is actually empty is
        empty on every attempt and still reported.

        A partial render is NOT retried: some panels drawn and some missing is a finding, and
        loading again would just average over it.
        """
        return self.retryable or self.empty


def classify(state: dict, min_headers: int = 1) -> Verdict:
    """Judge one sampled page.

    `state` carries the fields the evaluate payload collects: `ready`, `path`, `testids`,
    `headers`, `rows`, `statusError`, `pluginNotFound`. `min_headers` is how many rendered
    panel headers this dashboard must show; pass 0 for a row-only dashboard, which is then
    required to show at least one row instead.
    """
    if state.get("testids", 0) < _MOUNTED_MIN_TESTIDS or _is_bare_dashboard_url(
        state.get("path", "")
    ):
        return Verdict(
            NOT_MOUNTED,
            f"the dashboard never mounted — {state.get('testids', 0)} data-testid "
            f"attributes at readyState={state.get('ready')!r}, url {state.get('path')!r}",
        )

    if state.get("pluginNotFound"):
        return Verdict(
            FLAGGED,
            "the page carries 'Panel plugin not found' — a panel type this Grafana no "
            "longer implements is still provisioned",
        )

    # The message decides, not the count: Grafana marks an empty panel with the same testid as a
    # real failure.
    texts = list(dict.fromkeys(state.get("errorTexts") or []))
    real = [t for t in texts if t.strip().casefold() not in _BENIGN_PANEL_MESSAGES]
    if real:
        return Verdict(
            FLAGGED,
            f"{state.get('statusError', len(real))} panel(s) rendered in an error "
            f"state: " + "; ".join(real),
        )

    # Name the page: a lapsed session lands on Grafana's login form, which mounts and draws no
    # panels.
    where = f"(url {state.get('path')!r}, {state.get('testids')} testids)"

    headers = state.get("headers", 0)
    drew_nothing = not headers and not state.get("rows", 0)
    if min_headers:
        if headers < min_headers:
            return Verdict(
                FLAGGED,
                f"the dashboard mounted but drew {headers} panel header(s), expected at "
                f"least {min_headers} — panels are provisioned and not rendering {where}",
                empty=drew_nothing,
            )
    elif not state.get("rows", 0):
        return Verdict(
            FLAGGED,
            f"a row-only dashboard mounted but drew no rows at all {where}",
            empty=drew_nothing,
        )

    return Verdict(OK, f"{headers} panel header(s), {state.get('rows', 0)} row(s)")
