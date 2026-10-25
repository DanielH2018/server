"""Queue Uptime Kuma's Discord notifications and deliver them inside Discord's rate limit (#5657).

Kuma posts each notification once and never retries (`server/model/monitor.js`,
`sendNotification`, at the pinned 2.5.5). A mass state change sends dozens of posts within a
second, Discord answers the overflow with `429 You are being rate limited` and a `retry_after`
of a fraction of a second, and Kuma drops every one of them: 5 on 2026-09-06 (#1342), 6 on
2026-10-18 (#5657). This sidecar listens on the pod's loopback. The "Homelab Alerts"
notification posts here instead of to Discord, the relay answers at once and queues the body,
and one worker thread forwards the queue to the webhook in order. It honours
`X-RateLimit-Remaining: 0` before the next send, and sleeps `retry_after` on a 429 and retries.

The answer is immediate because Kuma awaits the send inside the push route
(`server/routers/api-router.js`) before it answers the pusher, and pushers give up after 10s.
A relay that held each request until Discord accepted it would turn a mass flip into push
timeouts.

A message the relay cannot deliver is logged as one `Cannot send notification to Homelab
Alerts` line, the text Kuma writes for its own drops, so the loki-homelab rule
`KumaNotificationDropped` counts both containers. That covers a message past its deadline, a
4xx other than 429 and a message still queued when the pod stops. A full queue answers 503,
so Kuma itself logs that drop.

Stdlib only: it runs in the python image the role's CronJobs already pin. The script ships in a
ConfigMap that Flux substitutes into, so it holds no dollar sign.
"""

import http.server
import json
import os
import queue
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

NOTIFICATION = "Homelab Alerts"
# Discord's edge refuses a request with urllib's default User-Agent (Cloudflare error 1010,
# answered as a 403), so every post names this relay.
USER_AGENT = "kuma-discord-relay"
HTTP_TIMEOUT = 10.0
MAX_BACKOFF = 30.0


def post(url: str, body: bytes, content_type: str) -> tuple[int | None, dict, bytes]:
    """POST one body. Returns (status, headers, response body); status None is a network error."""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()
    except (urllib.error.URLError, OSError) as error:
        return None, {}, str(error).encode()


def _header(headers: dict, name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def retry_after(headers: dict, body: bytes) -> float:
    """Seconds a 429 asks for: the JSON `retry_after`, else the `Retry-After` header, else 1."""
    try:
        value = json.loads(body).get("retry_after")
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    except ValueError, AttributeError:
        pass
    try:
        return max(float(_header(headers, "Retry-After") or 1.0), 0.0)
    except ValueError:
        return 1.0


def bucket_wait(headers: dict) -> float:
    """Seconds to hold the next send after a success that emptied the rate-limit bucket."""
    if _header(headers, "X-RateLimit-Remaining") != "0":
        return 0.0
    try:
        return max(float(_header(headers, "X-RateLimit-Reset-After") or 0.0), 0.0)
    except ValueError:
        return 0.0


class Relay:
    """The queue and the delivery loop, with the transport, the clock and the log injectable."""

    def __init__(
        self,
        url_file: str,
        max_queue: int,
        deadline: float,
        send=post,
        sleep=time.sleep,
        clock=time.monotonic,
        log=print,
    ):
        self.url_file = url_file
        self.deadline = deadline
        self.queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self.send = send
        self.sleep = sleep
        self.clock = clock
        self.log = log
        self.in_flight = 0

    def accept(self, body: bytes, content_type: str) -> bool:
        """Queue one notification; False when the queue is full."""
        try:
            self.queue.put_nowait((self.clock(), body, content_type))
        except queue.Full:
            self.log(f"queue full ({self.queue.maxsize}), refusing a notification")
            return False
        return True

    def drop(self, reason: str) -> None:
        self.log(f"Cannot send notification to {NOTIFICATION}: discord-relay {reason}")

    def webhook_url(self) -> str:
        # Read on every send, so a rotated URL reaches the relay once the kubelet refreshes the
        # Secret volume, with no restart.
        try:
            with open(self.url_file, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def deliver(self, enqueued: float, body: bytes, content_type: str) -> None:
        """Send one notification, retrying a 429, a 5xx and a network error until the deadline."""
        failures = 0
        while True:
            url = self.webhook_url()
            if not url:
                self.drop(f"has no webhook URL in {self.url_file}")
                return
            status, headers, answer = self.send(url, body, content_type)
            if status is not None and 200 <= status < 300:
                wait = bucket_wait(headers)
                if wait:
                    self.sleep(wait)
                return
            if status is not None and 400 <= status < 500 and status != 429:
                self.drop(f"was refused with HTTP {status}: {answer[:200]!r}")
                return
            if status == 429:
                wait = retry_after(headers, answer)
            else:
                failures += 1
                wait = min(2.0**failures, MAX_BACKOFF)
            if self.clock() + wait - enqueued > self.deadline:
                last = (
                    f"HTTP {status}"
                    if status is not None
                    else answer[:200].decode(errors="replace")
                )
                self.drop(f"gave up after {self.deadline:.0f}s, last answer {last}")
                return
            self.sleep(wait)

    def run_once(self, timeout: float | None = None) -> bool:
        """Deliver the next queued notification; False when none arrived within `timeout`."""
        try:
            enqueued, body, content_type = self.queue.get(timeout=timeout)
        except queue.Empty:
            return False
        self.in_flight = 1
        try:
            self.deliver(enqueued, body, content_type)
        finally:
            self.in_flight = 0
            self.queue.task_done()
        return True

    def work(self) -> None:
        while True:
            self.run_once()

    def abandon(self) -> int:
        """Log a drop for every notification the relay will not deliver; returns the count."""
        undelivered = self.in_flight
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
            undelivered += 1
        for _ in range(undelivered):
            self.drop("stopped with the notification still queued")
        return undelivered


def make_handler(relay: Relay, worker: threading.Thread):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            content_type = self.headers.get("Content-Type") or "application/json"
            accepted = relay.accept(body, content_type)
            self.send_response(204 if accepted else 503)
            self.end_headers()

        def do_GET(self):
            # The liveness probe: the listener answers while the worker that drains it is dead.
            self.send_response(200 if worker.is_alive() else 503)
            self.end_headers()

        def log_message(self, format, *args):
            return

    return Handler


def main() -> int:
    relay = Relay(
        url_file=os.environ["RELAY_WEBHOOK_URL_FILE"],
        max_queue=int(os.environ.get("RELAY_MAX_QUEUE", "500")),
        deadline=float(os.environ.get("RELAY_DEADLINE_SECONDS", "900")),
        log=lambda line: print(line, flush=True),
    )
    worker = threading.Thread(target=relay.work, name="deliver", daemon=True)
    worker.start()
    port = int(os.environ["RELAY_PORT"])
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(relay, worker)
    )

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    print(f"discord-relay listening on 127.0.0.1:{port}", flush=True)
    server.serve_forever()
    relay.abandon()
    return 0


if __name__ == "__main__":
    sys.exit(main())
