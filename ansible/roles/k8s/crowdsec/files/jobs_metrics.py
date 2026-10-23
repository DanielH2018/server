"""The crowdsec-jobs sidecar's check exporter: each Kuma-pushed job's verdict, served as gauges.

`crowdsec_jobs.py` records one verdict per run of a job that pushes to Kuma, and this module
serves them on `/metrics` beside the push. It is M6's pod exporter
(docs/target-architecture/monitoring/m6-retire-kuma.md, *B6b*), and it uses the same series
names as monitor-bridge's check exporter (`verdict_export.py`), so one rule shape reads both:

- `homelab_check_success{check="<tag>"}` is 1 when the job's latest run exited 0 and 0 when it
  did not. The series appears with the job's first run.
- `homelab_check_last_run_timestamp_seconds{check="<tag>"}` is when that run ended. Before the
  first run it is the time the exporter started, so a restart does not read as a job that has
  not run for 56 years, and a job that never runs ages past its deadline all the same.
- `homelab_check_deadline_seconds{check="<tag>"}` is how old that timestamp may grow: three
  periods of the job.
- `homelab_check_rows` is the number of jobs the exporter covers.

The stdlib only, because the sidecar runs the repo's pinned python image with no packages.
"""

import threading
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Periods a job's timestamp may age before it reads stale. One missed run is a slow job and two
# is a hung one, so the third is the first that is not noise.
DEADLINE_PERIODS = 3

ROWS = ("homelab_check_rows", "Jobs the crowdsec-jobs exporter covers.")
SUCCESS = (
    "homelab_check_success",
    "1 when the job's latest run exited 0, 0 when it did not.",
)
TIMESTAMP = (
    "homelab_check_last_run_timestamp_seconds",
    "Unix time the job's latest run ended, or the exporter's start before the first run.",
)
DEADLINE = (
    "homelab_check_deadline_seconds",
    "Seconds the job's timestamp may age before it is stale.",
)


class Exposition:
    """The scrape body for the jobs' verdicts. A run replaces its job's row in place."""

    def __init__(
        self, deadlines: Mapping[str, float], started: float | None = None
    ) -> None:
        """`deadlines` maps each covered job's tag to the seconds its timestamp may age."""
        start = time.time() if started is None else started
        self._deadlines = dict(deadlines)
        self._success: dict[str, float] = {}
        self._timestamp = {tag: start for tag in self._deadlines}
        self._lock = threading.Lock()

    def record(self, tag: str, ok: bool, now: float) -> None:
        """Record the latest run of `tag`. A tag the exporter does not cover is ignored."""
        with self._lock:
            if tag in self._deadlines:
                self._success[tag] = 1.0 if ok else 0.0
                self._timestamp[tag] = now

    def body(self) -> bytes:
        """The text a scrape receives."""
        with self._lock:
            success = dict(self._success)
            timestamp = dict(self._timestamp)
        lines = [
            "# HELP %s %s" % ROWS,
            "# TYPE %s gauge" % ROWS[0],
            "%s %d" % (ROWS[0], len(self._deadlines)),
        ]
        for (metric, help_text), values in (
            (SUCCESS, success),
            (TIMESTAMP, timestamp),
            (DEADLINE, self._deadlines),
        ):
            if not values:
                continue
            lines += ["# HELP %s %s" % (metric, help_text), "# TYPE %s gauge" % metric]
            lines += [
                '%s{check="%s"} %r' % (metric, tag, float(value))
                for tag, value in sorted(values.items())
            ]
        return ("\n".join(lines) + "\n").encode()

    def serve(self, port: int) -> ThreadingHTTPServer:
        """Serve `GET /metrics` on `port`, on all addresses, from a daemon thread.

        Raises:
          OSError: the port could not be bound.
        """
        exposition = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] != "/metrics":
                    self.send_error(404)
                    return
                body = exposition.body()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                # A scrape a minute would put a line a minute into the container log.
                pass

        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
