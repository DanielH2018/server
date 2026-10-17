"""C7's reference time for an `owner: flux` tag, against a revision that leaves the tag unchanged (#5338).

Every per-tag Kustomization tracks the shared `homelab` source, so its `revision` label moves
on every master commit. The reference must move only when the tag's own rendered content does.

The fake Prometheus holds the per-minute sample history of the tag's `gotk_resource_info`
series. Each sample carries `revision` and `CONTENT_LABEL`, the digest of the tag's rendered
content, which changes only when the tag does. A query is answered by what it means over that
history, not by a canned value:

  - `min(min_over_time(timestamp(<sel>)[..]) and on(<labels>) <sel>)`: the earliest sample
    that agrees with the newest one on `<labels>`;
  - `<sel>[<range>]` or `<sel>[<range>:<step>]`: a matrix, one series per label set;
  - `<sel>`: the newest sample as a vector.

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_flux_reference.py
"""

import re

from diagnostics.probe_lib import health_alerts

TAG = "peanut"
PROM_START = 900_000.0
T1 = 1_000_000.0  # the tag's content first applied
_MONTH_OLD = (
    "2026-09-16T00:00:00Z"  # the release record froze at the last Ansible apply
)

# The C6 label naming the digest of a tag's own rendered content. A revision bump that leaves
# the tag unchanged keeps it.
CONTENT_LABEL = "content_digest"

_ON = re.compile(r"and on\(([^)]*)\)")
_RANGE = re.compile(r"gotk_resource_info\{[^}]*\}\[[^\]]+\]")


class FluxHistoryProm:
    """A Prometheus holding one tag's `gotk_resource_info` history and, optionally, one alert.

    `revisions` is a list of `(first_reported, revision, content)`; each holds until the next
    one starts, sampled every minute up to `now`.
    """

    def __init__(self, revisions, now, alert_active=None):
        self.revisions = revisions
        self.now = now
        self.alert_active = alert_active

    def _samples(self):
        out = []
        for i, (start, revision, content) in enumerate(self.revisions):
            end = (
                self.revisions[i + 1][0]
                if i + 1 < len(self.revisions)
                else self.now + 1
            )
            labels = {
                "__name__": "gotk_resource_info",
                "customresource_kind": "Kustomization",
                "exported_namespace": "flux-system",
                "name": TAG,
                "ready": "True",
                "source_name": "homelab",
                "revision": revision,
                CONTENT_LABEL: content,
            }
            ts = start
            while ts < end:
                out.append((ts, labels))
                ts += 60
        return out

    def _flux(self, promql):
        if f'name="{TAG}"' not in promql:
            return []
        samples = self._samples()
        newest = samples[-1][1]
        if promql.startswith("min("):
            on = _ON.search(promql)
            keys = [k.strip() for k in on.group(1).split(",")] if on else []
            first = min(
                ts for ts, lab in samples if all(lab[k] == newest[k] for k in keys)
            )
            return [{"metric": {}, "value": [self.now, str(first)]}]
        if _RANGE.search(promql):
            series = {}
            for ts, lab in samples:
                key = frozenset(lab.items())
                series.setdefault(key, {"metric": lab, "values": []})
                series[key]["values"].append([ts, "1"])
            return list(series.values())
        if promql.startswith("gotk_resource_info{"):
            return [{"metric": newest, "value": [self.now, "1"]}]
        raise AssertionError(f"the fake cannot answer {promql}")

    def query(self, promql):
        if "gotk_resource_info" in promql:
            return self._flux(promql)
        if self.alert_active is None:
            return []
        labels = {"alertname": "SomethingBroke", "severity": "critical", "service": TAG}
        if promql.startswith("ALERTS_FOR_STATE"):
            return [{"metric": labels, "value": [self.now, str(self.alert_active)]}]
        if promql.startswith("ALERTS{") and 'verify="true"' not in promql:
            firing = {"__name__": "ALERTS", **labels, "alertstate": "firing"}
            return [{"metric": firing, "value": [self.now, "1"]}]
        if promql.startswith("process_start_time_seconds"):
            return [{"metric": {}, "value": [self.now, str(PROM_START)]}]
        return []

    def tools(self):
        def sleep(seconds):
            self.now += seconds

        return health_alerts.PromTools(
            query=self.query, sleep=sleep, clock=lambda: self.now
        )


def test_a_flux_reference_holds_across_a_revision_that_leaves_the_tag_unchanged():
    """An unrelated master commit keeps the reference; a change to the tag moves it."""
    t2, t3 = T1 + 420, T1 + 1200
    revisions = [
        (T1, "master@sha1:aaaa", "sha256:x"),
        (t2, "master@sha1:bbbb", "sha256:x"),
    ]
    fake = FluxHistoryProm(revisions, now=t2 + 300)
    assert health_alerts.alert_reference(TAG, "flux", _MONTH_OLD, fake.query) == T1

    fake = FluxHistoryProm(
        [*revisions, (t3, "master@sha1:cccc", "sha256:y")], now=t3 + 300
    )
    assert health_alerts.alert_reference(TAG, "flux", _MONTH_OLD, fake.query) == t3


def test_an_alert_after_the_content_change_still_fails_once_an_unrelated_commit_reconciles():
    """The issue's window: a bad deploy fires at A, then an unrelated revision lands after A."""
    active, t2 = T1 + 120, T1 + 420
    fake = FluxHistoryProm(
        [(T1, "master@sha1:aaaa", "sha256:x"), (t2, "master@sha1:bbbb", "sha256:x")],
        now=t2 + 300,
        alert_active=active,
    )
    code, lines = health_alerts.check(
        TAG, "flux", _MONTH_OLD, T1 + 60, {"name": TAG}, fake.tools()
    )
    assert code == 1, lines
    assert any("SomethingBroke" in line and "note:" not in line for line in lines)
