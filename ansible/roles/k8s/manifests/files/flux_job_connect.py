"""The one way a Job that Flux applies opens an HTTP connection: retry a refusal, never an answer.

A brand-new pod admitted by a NetworkPolicy selector peer can be refused until kube-router
learns its address (target slice D5, *Rules for every Job Flux applies*). So the first
connection a Job opens to a fenced Service retries a refused or timed-out connect for up to 60
seconds. Anything the server sends back is an answer and is never retried: an HTTP error
status, an empty reply, a reset after accept, a read that times out. Retrying those would let
a flaky check pass on its second try.

The Job census in `scripts/renderer/tests/test_renderer_job_census.py` fails a Job script that
opens a connection any other way, and it admits this file only as a byte-identical copy beside
the script in the Job's ConfigMap. Standard library only: the Job image is `python:3.14-alpine`.

Typical usage example:

    from flux_job_connect import open_url

    with open_url("http://sonarr.media.svc:8989/ping", timeout=10) as response:
        body = response.read()
"""

import time
import urllib.error
import urllib.request
from collections.abc import Callable

# How long a refused or timed-out connect keeps being retried, in seconds.
RETRY_SECONDS = 60
# The pause between two connect attempts, in seconds.
PAUSE_SECONDS = 2


def refused(err: BaseException) -> bool:
    """Whether `err` means the connect itself failed, so no server has answered yet.

    `urlopen` wraps an error raised while connecting or sending the request in a `URLError`
    whose `reason` is the socket error. An error raised while reading the response is not
    wrapped, and an `HTTPError` is a `URLError` that carries an answer.
    """
    if isinstance(err, urllib.error.HTTPError) or not isinstance(
        err, urllib.error.URLError
    ):
        return False
    return isinstance(err.reason, (ConnectionRefusedError, TimeoutError))


def open_url(
    request: str | urllib.request.Request,
    *,
    timeout: float,
    retry_seconds: float = RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
):
    """Open `request` with `urllib.request.urlopen`, retrying while the connect is refused.

    Args:
        request: A URL, or a `Request` carrying a method, headers or a body.
        timeout: The socket timeout of each attempt, in seconds.
        retry_seconds: How long after the first attempt a refused connect is still retried.
        sleep: Waits between attempts; a test passes its own.
        clock: A monotonic clock; a test passes its own.

    Returns:
        The response `urlopen` returns, which the caller closes.

    Raises:
        urllib.error.URLError: The connect was still refused or timing out after
            `retry_seconds`, or it failed for another reason.
        urllib.error.HTTPError: The server answered with an error status.
        OSError: The server accepted the connection and then sent no usable answer, such as
            `http.client.RemoteDisconnected` or a read timeout.
    """
    deadline = clock() + retry_seconds
    while True:
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except OSError as err:
            if not refused(err) or clock() >= deadline:
                raise
        sleep(PAUSE_SECONDS)
