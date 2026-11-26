"""The check exporter: per-row verdict gauges for Prometheus.

Each 300s cycle `check.run_once` and `gates._gate` record one verdict per enabled row of
`check_table.CHECKS`. `CheckExposition` serves them to observability's `monitor-bridge` scrape
job as three gauges per row, labelled `check="<row>"`, and one gauge for the whole table:

- `homelab_check_success` is 1 when the row's verdict is up and 0 when it is down.
- `homelab_check_last_run_timestamp_seconds` is when the cycle recorded the row.
- `homelab_check_deadline_seconds` is how old that timestamp may grow before the row is stale:
  three cycles. `INTERVAL` is 300s, so 900s.
- `homelab_check_rows` is the count of enabled rows in `CHECKS`, gate rows included. It is not
  `registry.build_checks`' length, because that drops every gate row.
- `homelab_check_detail{check="<row>", detail="<member>"}` is 1 for each member a down row names
  in its result's `details`: an indexer, a drive, an R2 arm. An up row has none, so a summary
  that reads it falls back to its category text. Each detail is a member's name and never a
  count, so the series stay the same from one cycle to the next.
- `homelab_domain_registration_expiry_days` is the days left before the domain's registration
  expires, from the `domain_registration` row's RDAP read. It is absent until a read succeeds.

The value is the end state's verdict, taken before suppression
(docs/archive/monitoring/m6-retire-kuma.md, *The bridge as an exporter*). A row whose
gate reads down exports 0 with a fresh timestamp and its body is not called.

It reuses `deploy_pipeline.Exposition`'s listener, on its own port so each scrape job sees only
its own family.
"""

import threading

from deploy_pipeline import Exposition

# Cycles a row's timestamp may age before it reads stale.
DEADLINE_CYCLES = 3

ROWS_GAUGE = (
    "homelab_check_rows",
    "Enabled rows of the check table, gate rows included.",
)
SUCCESS_GAUGE = (
    "homelab_check_success",
    "1 when the row's latest verdict is up, 0 when down.",
)
TIMESTAMP_GAUGE = (
    "homelab_check_last_run_timestamp_seconds",
    "Unix time the cycle last recorded the row.",
)
REGISTRATION_GAUGE = (
    "homelab_domain_registration_expiry_days",
    "Days until the domain's registration expires, from its RDAP record.",
)
DEADLINE_GAUGE = (
    "homelab_check_deadline_seconds",
    "Seconds the row's timestamp may age before it is stale.",
)
DETAIL_GAUGE = (
    "homelab_check_detail",
    "1 for each member a down row names as failing.",
)


def _label(value: str) -> str:
    """`value` escaped for a label in the text exposition format."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class CheckExposition(Exposition):
    """The scrape body for the per-row gauges: each row's latest verdict, and the row count.

    A row is replaced in place each cycle. A row that stops being recorded keeps its last
    timestamp and ages past its deadline, which is how a hung loop becomes visible.
    """

    def __init__(self, interval_s: float) -> None:
        super().__init__()
        self._rows = 0
        self._deadline = float(DEADLINE_CYCLES * interval_s)
        self._verdicts: dict[str, tuple[float, float]] = {}
        self._details: dict[str, tuple[str, ...]] = {}
        self._registration_days: float | None = None
        self._verdict_lock = threading.Lock()
        self._render()

    def set_rows(self, rows: int) -> None:
        """Set the row count: the enabled rows of `check_table.CHECKS`, gate rows included."""
        self._rows = rows
        self._render()

    def set_registration_days(self, days: float | None) -> None:
        """Set the days left on the domain's registration. None drops the gauge."""
        self._registration_days = days
        self._render()

    def record(
        self, name: str, ok: bool, now: float, details: tuple[str, ...] = ()
    ) -> None:
        """Record `name`'s verdict for this cycle and republish the body.

        Args:
          name: The row's check name.
          ok: The row's verdict.
          now: When the cycle recorded it.
          details: The failing members `CheckResult.details` names. Served only while `ok` is
            false, and replaced on every record, so a recovered row drops its series.
        """
        with self._verdict_lock:
            self._verdicts[name] = (1.0 if ok else 0.0, now)
            self._details[name] = () if ok else details
        self._render()

    def _render(self) -> None:
        with self._verdict_lock:
            verdicts = sorted(self._verdicts.items())
            details = sorted(
                (name, detail)
                for name, members in self._details.items()
                for detail in members
            )
        lines = [
            "# HELP %s %s" % ROWS_GAUGE,
            "# TYPE %s gauge" % ROWS_GAUGE[0],
            "%s %d" % (ROWS_GAUGE[0], self._rows),
        ]
        if self._registration_days is not None:
            lines += [
                "# HELP %s %s" % REGISTRATION_GAUGE,
                "# TYPE %s gauge" % REGISTRATION_GAUGE[0],
                "%s %r" % (REGISTRATION_GAUGE[0], float(self._registration_days)),
            ]
        if verdicts:
            for (metric, help_text), pick in (
                (SUCCESS_GAUGE, lambda v: v[0]),
                (TIMESTAMP_GAUGE, lambda v: v[1]),
                (DEADLINE_GAUGE, lambda v: self._deadline),
            ):
                lines += [
                    "# HELP %s %s" % (metric, help_text),
                    "# TYPE %s gauge" % metric,
                ]
                lines += [
                    '%s{check="%s"} %r' % (metric, name, float(pick(v)))
                    for name, v in verdicts
                ]
        if details:
            lines += [
                "# HELP %s %s" % DETAIL_GAUGE,
                "# TYPE %s gauge" % DETAIL_GAUGE[0],
            ]
            lines += [
                '%s{check="%s",detail="%s"} 1' % (DETAIL_GAUGE[0], name, _label(d))
                for name, d in details
            ]
        body = ("\n".join(lines) + "\n").encode()
        with self._lock:
            self._body = body
