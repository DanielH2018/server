"""The check exporter: per-row verdict gauges for Prometheus, served beside the Kuma push.

Each 300s cycle `check.run_once` and `gates._gate` record one verdict per enabled row of
`check_table.CHECKS`. `CheckExposition` serves them to observability's `monitor-bridge` scrape
job as three gauges per row, labelled `check="<row>"`, and one gauge for the whole table:

- `homelab_check_success` is 1 when the row's verdict is up and 0 when it is down.
- `homelab_check_last_run_timestamp_seconds` is when the cycle recorded the row.
- `homelab_check_deadline_seconds` is how old that timestamp may grow before the row is stale:
  three cycles. `INTERVAL` is 300s, so 900s.
- `homelab_check_rows` is the count of enabled rows in `CHECKS`, gate rows included. It is not
  `registry.build_checks`' length, because that drops every gate row.

The value is the end state's verdict, taken before suppression and before `startup_grace`
(docs/target-architecture/monitoring/m6-retire-kuma.md, *The bridge as an exporter*). A row whose
gate reads down exports 0 with a fresh timestamp and its body is not called. The push keeps
today's values beside it until the push path is deleted.

It reuses `deploy_pipeline.Exposition`'s listener, on its own port so each scrape job sees only
its own family.
"""

import threading

from deploy_pipeline import Exposition

# Cycles a row's timestamp may age before it reads stale. One missed cycle is a slow body and
# two is a hung one, so the third is the first that is not noise.
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
DEADLINE_GAUGE = (
    "homelab_check_deadline_seconds",
    "Seconds the row's timestamp may age before it is stale.",
)


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
        self._verdict_lock = threading.Lock()
        self._render()

    def set_rows(self, rows: int) -> None:
        """Set the row count: the enabled rows of `check_table.CHECKS`, gate rows included."""
        self._rows = rows
        self._render()

    def record(self, name: str, ok: bool, now: float) -> None:
        """Record `name`'s verdict for this cycle and republish the body."""
        with self._verdict_lock:
            self._verdicts[name] = (1.0 if ok else 0.0, now)
        self._render()

    def _render(self) -> None:
        with self._verdict_lock:
            verdicts = sorted(self._verdicts.items())
        lines = [
            "# HELP %s %s" % ROWS_GAUGE,
            "# TYPE %s gauge" % ROWS_GAUGE[0],
            "%s %d" % (ROWS_GAUGE[0], self._rows),
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
        body = ("\n".join(lines) + "\n").encode()
        with self._lock:
            self._body = body
