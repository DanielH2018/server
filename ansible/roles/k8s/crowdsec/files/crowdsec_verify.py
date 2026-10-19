"""crowdsec's verify Job: ban daniel-pi at LAPI, read the edge's 403s off-cluster, lift the ban.

Inert until crowdsec's flip, in `templates/verify-inert/` (target slice D5, *crowdsec's verify
from outside the cluster*, and its Open question 9, confirmed by the operator on 2026-10-18).
It replaces the gate in `tasks/verify.yml`, "Ban daniel-pi and prove the edge enforces it",
without the `pods/exec` grant that gate's `cscli` calls would need as a Job. Each hop moves:

- **The ban** goes to LAPI's HTTP API as a machine: `POST /v1/watchers/login`, then `POST
  /v1/alerts` carrying one `ban` decision, the request `cscli decisions add` sends. Reading
  it back is `GET /v1/alerts?ip=...&has_active_decision=true`.
- **The bouncer's pull has no Job form.** LAPI v1.8.1 serves no bouncer route to a machine,
  so `last_pull` cannot be read, and the gate's hop 2 folds into hop 3: a 403 at the edge
  proves the pull happened. A failure therefore names both causes.
- **The edge's answer** is read from Prometheus, never from the Pi over the network. The
  `crowdsec-edge-probe` timer on daniel-pi probes the VIP every minute, the Pi's Alloy counts
  each answer by status, and Prometheus scrapes that counter straight off the Pi as
  `alloy-pi`. The ban makes Traefik refuse the Pi, so evidence that crossed the edge would be
  lost; this path never crosses it.
- **The lift** is `DELETE /v1/decisions?ip=...`, in a `finally`, so every exit path after the
  ban lifts it.

The verdict, once the ban reads back from LAPI:

- pass as soon as the 403 count rises above its value before the ban;
- at the window's end, a failure naming **the channel** when `up{job="alloy-pi"}` is not 1 or
  no probe answer of any status was counted in the window, because nothing was observed;
- otherwise a failure naming **the edge**, with the statuses it answered while banned.

The window covers one probe period, one 60-second scrape and the 30-second scrape timeout,
with slack. Inputs come from the Job's environment: `LAPI_URL`, `LAPI_MACHINE`,
`LAPI_PASSWORD`, `BAN_IP`, `BAN_DURATION`, `PROMETHEUS_URL` and `WINDOW_SECONDS`. Exit 0 when
the edge enforced the ban, 1 otherwise, naming the hop.

Run: python3 crowdsec_verify.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/crowdsec/tests/test_crowdsec_verify.py
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from flux_job_connect import Request, open_url

COUNTER = "loki_process_custom_crowdsec_edge_probe_total"
CHANNEL_UP = 'up{job="alloy-pi"}'
# The seconds each call may take, and between two reads of the counter.
CALL_TIMEOUT = 10
POLL_SECONDS = 15
SCENARIO = "crowdsec-verify: ban daniel-pi to prove the edge enforces it"


class Failed(Exception):
    """A hop failed; the message names it."""


class Http:
    """JSON over HTTP, through `open_url`.

    Attributes:
        opener: Opens a `Request`; `open_url` unless a test passes its own.
    """

    def __init__(self, opener: Callable = open_url):
        self.opener = opener

    def call(
        self,
        method: str,
        url: str,
        body: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, object]:
        """`method` on `url`; the HTTP status and the parsed body, an error status included."""
        data = None if body is None else json.dumps(body).encode()
        request = Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            with self.opener(request, timeout=CALL_TIMEOUT) as response:
                return response.status, _parse(response.read())
        except urllib.error.HTTPError as err:
            with err:
                return err.code, _parse(err.read())


def _parse(raw: bytes) -> object:
    try:
        return json.loads(raw or b"null")
    except ValueError:
        return None


class Lapi:
    """LAPI's machine API, logged in as one machine."""

    def __init__(self, http: Http, base: str, machine: str, password: str):
        self.http, self.base = http, base.rstrip("/")
        status, body = http.call(
            "POST",
            f"{self.base}/v1/watchers/login",
            {"machine_id": machine, "password": password, "scenarios": []},
        )
        token = body.get("token") if isinstance(body, dict) else None
        if status != 200 or not token:
            raise Failed(f"LAPI refused the login of machine {machine}: HTTP {status}")
        self.auth = {"Authorization": f"Bearer {token}"}

    def ban(self, ip: str, duration: str, now: datetime) -> None:
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        decision = {
            "duration": duration,
            "origin": "cscli",
            "scenario": SCENARIO,
            "scope": "Ip",
            "type": "ban",
            "value": ip,
        }
        alert = {
            "capacity": 0,
            "decisions": [decision],
            "events": [],
            "events_count": 1,
            "leakspeed": "0",
            "message": SCENARIO,
            "scenario": SCENARIO,
            "scenario_hash": "",
            "scenario_version": "",
            "simulated": False,
            "source": {"ip": ip, "scope": "Ip", "value": ip},
            "start_at": stamp,
            "stop_at": stamp,
            "remediation": True,
        }
        status, _ = self.http.call("POST", f"{self.base}/v1/alerts", [alert], self.auth)
        if status not in (200, 201):
            raise Failed(f"LAPI refused the ban on {ip}: HTTP {status}")

    def banned(self, ip: str) -> bool:
        query = urllib.parse.urlencode({"ip": ip, "has_active_decision": "true"})
        status, body = self.http.call(
            "GET", f"{self.base}/v1/alerts?{query}", headers=self.auth
        )
        return status == 200 and bool(body)

    def lift(self, ip: str) -> None:
        """Delete every decision on `ip`, printing a refusal rather than raising it.

        The lift runs in a `finally` and must not hide the hop that failed. A refused lift
        leaves the ban to lapse at its duration.
        """
        query = urllib.parse.urlencode({"ip": ip})
        try:
            status, _ = self.http.call(
                "DELETE", f"{self.base}/v1/decisions?{query}", headers=self.auth
            )
            why = "" if status == 200 else f"HTTP {status}"
        except OSError as err:
            # A refused connect past open_url's retries, a timeout or a dropped reply. Raising
            # it out of the `finally` would replace the verdict with a traceback.
            why = f"no answer ({err!r})"
        if why:
            print(
                f"crowdsec-verify: LAPI did not lift the ban on {ip}: {why}; the ban"
                " lapses at its duration",
                file=sys.stderr,
            )


def prometheus(http: Http, base: str, query: str) -> list[dict]:
    """An instant query's result vector."""
    url = f"{base.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
    status, body = http.call("GET", url)
    if status != 200 or not isinstance(body, dict):
        raise Failed(
            f"the evidence channel: Prometheus answered HTTP {status} to {query}"
        )
    return (body.get("data") or {}).get("result") or []


def counts(http: Http, base: str) -> dict[str, float]:
    """The probe counter summed per `status` label."""
    found: dict[str, float] = {}
    for series in prometheus(http, base, COUNTER):
        status = series.get("metric", {}).get("status", "")
        found[status] = found.get(status, 0.0) + float(series["value"][1])
    return found


def channel_up(http: Http, base: str) -> bool:
    return any(float(s["value"][1]) == 1 for s in prometheus(http, base, CHANNEL_UP))


def increase(
    before: Mapping[str, float], after: Mapping[str, float]
) -> dict[str, float]:
    """Each status's rise; a counter that fell was reset, so its whole value is the rise."""
    rises = {}
    for status, value in after.items():
        old = before.get(status, 0.0)
        rise = value - old if value >= old else value
        if rise > 0:
            rises[status] = rise
    return rises


def run(
    env: Mapping[str, str],
    http: Http,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Ban, wait for the edge's 403s, and lift; the line that proves the edge enforced it.

    Raises:
        Failed: A hop failed; the message names it.
    """
    ip, prom = env["BAN_IP"], env["PROMETHEUS_URL"]
    if not channel_up(http, prom):
        raise Failed(
            'the evidence channel: up{job="alloy-pi"} is not 1, so Prometheus cannot read'
            " the Pi's probe counter; the edge was not tested"
        )
    before = counts(http, prom)
    lapi = Lapi(http, env["LAPI_URL"], env["LAPI_MACHINE"], env["LAPI_PASSWORD"])
    lapi.ban(ip, env["BAN_DURATION"], now())
    try:
        if not lapi.banned(ip):
            raise Failed(f"LAPI holds no active decision for {ip} after the ban")
        deadline = clock() + float(env["WINDOW_SECONDS"])
        rises: dict[str, float] = {}
        while True:
            rises = increase(before, counts(http, prom))
            if rises.get("403"):
                return f"the edge answered 403 to {ip} {int(rises['403'])} time(s) while banned"
            if clock() >= deadline:
                break
            sleep(POLL_SECONDS)
        window = int(float(env["WINDOW_SECONDS"]))
        if not channel_up(http, prom) or not rises:
            raise Failed(
                f"the evidence channel: no probe answer from {ip} was counted in {window}s,"
                ' or up{job="alloy-pi"} fell; the edge was not tested'
            )
        seen = ", ".join(f"{s} x{int(n)}" for s, n in sorted(rises.items()))
        raise Failed(
            f"the edge: LAPI holds the ban on {ip}, but the edge answered {seen} and no"
            f" 403 in {window}s. Either the bouncer has not pulled since the ban (the LAPI"
            " log's GET /v1/decisions/stream lines show its pulls) or the edge does not"
            " consult it"
        )
    finally:
        lapi.lift(ip)


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    http: Http | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    try:
        line = run(os.environ if env is None else env, http or Http())
    except Failed as err:
        print(f"crowdsec-verify: {err}", file=sys.stderr)
        return 1
    except OSError as err:
        # A transport failure on any hop: say so as a verdict rather than dying with a
        # traceback. The `finally` in `run` has already tried the lift.
        print(
            f"crowdsec-verify: a call got no answer: {err!r}",
            file=sys.stderr,
        )
        return 1
    print(f"crowdsec-verify: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
