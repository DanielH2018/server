#!/usr/bin/env python3
"""Classify what a Grafana dashboard page actually rendered.

Pure functions over the payload `test_ui_smoke.py`'s browser evaluate returns, kept out of
that module so they can be tested WITHOUT a browser — the `ui` marker is deselected in CI,
so a classifier living only inside a `ui` test would never run there at all.

**Why a classifier and not a bare assertion.** Measured across the 19 provisioned dashboards
on 2026-08-30, a zero-panel read has two causes that look identical from an assertion and
must not be treated alike:

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
nothing. That is the shape of the 2026-08-22 incident, where 19 Angular `graph` panels were
provisioned to a Grafana that had dropped Angular and rendered nothing for 55 minutes while
the pod read 1/1 with zero errors in the log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Grafana is asked for `/d/<uid>/` and the client rewrites the URL to `/d/<uid>/<slug>` once
# it has the dashboard. A page still showing the bare form never got it, however much of
# Grafana's own chrome rendered around the hole — and the chrome alone is 27 to 53 testids,
# which is why a testid count cannot carry this on its own.
# Above a mid-load skeleton, well below Grafana's chrome. Measured 2026-08-30: a page caught
# part-way through loading carries 3 testids and can already have rewritten its URL, so the
# slug test alone lets it through; Grafana's navigation and menus alone are 27 to 53.
_MOUNTED_MIN_TESTIDS = 10
_BARE_DASHBOARD_URL = re.compile(r"^/d/[^/]+/?$")

# An empty panel is not a broken one. A 6h window can legitimately be quiet, so failing on
# this would make the tier flaky — and it is what a panel shows right after a Grafana
# restart, when the counters behind it start from nothing.
#
# The cost of that choice, stated rather than buried: a panel whose metric DIED reads "No
# data" too, and this tier will not catch it. That is the gap the claude-otel role's
# CLAUDE.md already names — `kopia_b2_billable_bytes` went away with Kopia and three panels
# returned nothing for two weeks behind a healthy pod. Catching it needs a per-panel
# expectation of what the metric should return, which is a different check from this one.
_BENIGN_PANEL_MESSAGES = {"no data"}


def _is_bare_dashboard_url(path: str) -> bool:
    return bool(_BARE_DASHBOARD_URL.match(path))


NOT_MOUNTED = "not_mounted"
OK = "ok"
FLAGGED = "flagged"


@dataclass(frozen=True)
class Verdict:
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
        worth a second load. Measured 2026-08-30: `crowdsec-details-per-machine` drew its 12
        rows in 2.1s on 6 of 6 isolated loads, and drew nothing as the fourth dashboard of a
        run in the same browser. Re-navigating cannot hide a real break, because a dashboard
        that is actually empty is empty on every attempt and still reported.

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

    # The MESSAGE decides, not the count. Grafana marks an empty panel with the same
    # `Panel status error` testid it uses for a real failure, so the count alone flags every
    # dashboard whose window happens to be quiet — 10 of `claude-code-otel`'s panels read
    # that way minutes after a Grafana restart. Distinct texts only: one broken query
    # repeated across ten panels should print one line, not ten.
    texts = list(dict.fromkeys(state.get("errorTexts") or []))
    real = [t for t in texts if t.strip().casefold() not in _BENIGN_PANEL_MESSAGES]
    if real:
        return Verdict(
            FLAGGED,
            f"{state.get('statusError', len(real))} panel(s) rendered in an error "
            f"state: " + "; ".join(real),
        )

    # Every failure below is about what the page drew, so it has to say WHICH page: a
    # session that lapsed lands back on Grafana's login form, which mounts perfectly well
    # and draws no panels — indistinguishable from a broken dashboard without the url.
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
