"""The soak Job's reads: the API server, source-controller's artifacts and Prometheus.

`flux_soak.py` judges R through a `Reads`, which a test replaces with fixtures, and the Job
builds the live one with `live_reads`. Every connection goes through
`flux_job_connect.open_url`, and the API server's CA reaches `urlopen` through `SSL_CERT_FILE`,
which the Job sets. A read raises `TransientError` for a 5xx or a dropped connection,
which `flux_soak` retries until its wait deadline, and `SoakError` for any other failure.

Run: imported only, by `flux_soak.py`, which the soak Job's ConfigMap carries it beside.
"""

import json
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from flux_job_connect import Request, open_url

API = "https://kubernetes.default.svc"
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"


class SoakError(Exception):
    """The soak cannot reach a verdict, or a read it must not narrow on failed."""


class TransientError(SoakError):
    """A read failed with a 5xx or a dropped connection, so a later read may succeed."""


def transient(err: OSError) -> bool:
    """Whether `err` is a 5xx answer or a refused, reset or aborted connection.

    A k3s upgrade restarts the API server under the soak, which then answers 503 or drops
    the connection for a few seconds. A 4xx is a real refusal and stays fatal.
    """
    if isinstance(err, urllib.error.HTTPError):
        return err.code >= 500
    if isinstance(err, urllib.error.URLError):
        return isinstance(err.reason, ConnectionError)
    return isinstance(err, ConnectionError)


def parse_time(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


@dataclass
class Reads:
    """Every read the soak makes, one callable each, so a test replaces them with fixtures.

    Attributes:
        api: the parsed JSON at an API server path. Raises SoakError on any failure.
        fetch: the bytes at an artifact URL. Raises SoakError on any failure.
        query: a PromQL instant query's `data.result`, at a Unix time or now.
        prometheus_start: when the Prometheus process started, as a Unix time.
        clock: the Unix time now.
        sleep: waits; a test passes its own.
    """

    api: Callable[[str], dict]
    fetch: Callable[[str], bytes]
    query: Callable[[str, float | None], list]
    prometheus_start: Callable[[], float]
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


def live_reads(
    prometheus: str,
    token_file: str = TOKEN_FILE,
    sleep: Callable[[float], None] = time.sleep,
) -> Reads:
    """The reads the Job makes, each through `open_url`, which waits on `sleep` to retry."""
    token = Path(token_file).read_text().strip()

    def get(request: Request | str, what: str) -> bytes:
        try:
            with open_url(request, timeout=30, sleep=sleep) as response:
                return response.read()
        except urllib.error.HTTPError as err:
            err.close()
            error = TransientError if transient(err) else SoakError
            raise error(f"{what}: HTTP {err.code}") from err
        except OSError as err:
            error = TransientError if transient(err) else SoakError
            raise error(f"{what}: {err}") from err

    def api(path: str) -> dict:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        return json.loads(get(Request(API + path, headers=headers), path))

    def query(promql: str, at: float | None) -> list:
        params = {"query": promql} | ({} if at is None else {"time": f"{at:.3f}"})
        body = json.loads(get(f"{prometheus}/api/v1/query?{urlencode(params)}", promql))
        if body.get("status") != "success":
            raise SoakError(f"{promql}: {body.get('error')}")
        return body["data"]["result"]

    def prometheus_start() -> float:
        url = f"{prometheus}/api/v1/status/runtimeinfo"
        return parse_time(json.loads(get(url, url))["data"]["startTime"])

    return Reads(api, lambda url: get(url, url), query, prometheus_start)
