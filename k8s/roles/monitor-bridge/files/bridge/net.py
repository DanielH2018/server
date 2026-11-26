"""HTTP, PromQL and LogQL fetching for monitor-bridge.

This is the transport. A check body never calls it: it queries through the `src` argument,
`bridge.sources.Sources`, whose live methods delegate here and whose fake answers in tests.
Every function that sends a request takes an `opener`, the `urllib.request.urlopen`-shaped callable that sends it, so a
transport test hands in a fake opener rather than patching `urllib.request`. None means
the real `urlopen`, resolved at call time. What the suite still patches on this module is
`_get_json` in the Loki parsing tests. Callers reach it as `bridge.net._get_json`, never by
from-import, because a from-import would copy the function into the caller's globals at import
time and the stub would change nothing that runs. That rule is enforced by
tests/services/monitor_bridge/test_bridge_patch_boundary.py.

CONFIGURATION IS A PARAMETER, NOT A GLOBAL. Every helper that reads a URL takes the frozen
`Config` as its FIRST argument, so this module holds no env-derived state and a test states
the configuration it wants by handing one in. `_get_json`, `_post_json` and `_instant_query`
read no configuration and take none.
"""

from collections.abc import Callable
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, TypeVar

from bridge.common import HTTP_TIMEOUT
from bridge.config import Config
from bridge.parsing import FETCH_BODY_MAX, describe_fetch_failure, endpoint_label
from bridge.types import JsonObject, JsonValue, as_list, as_object, as_object_list

Opener = Callable[..., Any]
# What `_get`'s `read` makes of the response: parsed JSON, or the raw bytes.
_Body = TypeVar("_Body")


# HTTP / parsing helpers (pure-ish, unit-tested)


def _get_json(
    url: str, headers: dict[str, str] | None = None, opener: Opener | None = None
) -> JsonValue:
    return _request(url, headers, opener, json.load)


def _get_bytes(
    url: str, headers: dict[str, str] | None = None, opener: Opener | None = None
) -> bytes:
    # A caller that hashes needs the raw bytes: an OCI manifest's digest covers the exact served
    # bytes, which a JSON round trip does not preserve.
    return _request(url, headers, opener, lambda resp: resp.read())


ETAG_CACHE_MAX = 8


def _get_json_conditional(
    url: str,
    headers: dict[str, str],
    cache: dict[str, tuple[str, JsonValue]],
    opener: Opener | None = None,
) -> JsonValue:
    """GET `url` with the ETag of its cached answer, and return that answer again on a 304.

    GitHub does not count a 304 to a request sent with an `Authorization` header against the
    primary rate limit, so a read whose data has not changed costs nothing.

    Args:
      url: The URL to read.
      headers: The request headers; `If-None-Match` is added when `cache` holds `url`.
      cache: `(ETag, body)` per URL, updated in place and kept to the `ETAG_CACHE_MAX` URLs
        read most recently. A response without an ETag is not cached.
      opener: Sends the request; None is the real `urlopen`.

    Raises:
      urllib.error.HTTPError: any status but a 304 answering a cached ETag.
      RuntimeError: as `_get_json`.
    """
    held = cache.get(url)
    hdrs = dict(headers)
    if held is not None:
        hdrs["If-None-Match"] = held[0]
    try:
        etag, body = _request(
            url, hdrs, opener, lambda resp: (resp.headers.get("ETag"), json.load(resp))
        )
    except urllib.error.HTTPError as e:
        if held is None or e.code != 304:
            raise
        etag, body = held
    cache.pop(url, None)
    if etag:
        cache[url] = (etag, body)
        while len(cache) > ETAG_CACHE_MAX:
            del cache[next(iter(cache))]
    return body


def _request(
    url: str,
    headers: dict[str, str] | None,
    opener: Opener | None,
    read: Callable[[Any], _Body],
    method: str = "GET",
    data: bytes | None = None,
) -> _Body:
    # The explicit User-Agent is REQUIRED: Cloudflare 403s the default python-urllib UA (error
    # 1010), so webhook GETs would read as revoked.
    # host_lib.discord_post has the same reason; the two cannot share a module.
    hdrs = {"User-Agent": "monitor-bridge"}
    if headers is not None:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=HTTP_TIMEOUT) as resp:
            return read(resp)
    except urllib.error.HTTPError as e:
        # Re-raise the SAME type: check_discord branches on `e.code`, so wrapping would turn a
        # decisive 404 into a generic "unreachable".
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            # The consumer reads .code and .msg only; an unclosed HTTPError holds the response until
            # GC.
            e.close()
        detail = " ".join((body or "").split())[:FETCH_BODY_MAX]
        e.msg = "%s: %s" % (endpoint_label(url), detail or e.msg)
        raise
    except Exception as e:
        raise RuntimeError(describe_fetch_failure(url, e)) from e


def _post_json(
    url: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    opener: Opener | None = None,
) -> JsonValue:
    """POST a JSON body and return the parsed JSON response. Same failure contract as _get_json.

    Only the Cloudflare GraphQL endpoint needs this — every other source here is a GET.
    """
    # Same required User-Agent as _get_json above, for the same Cloudflare 1010 reason.
    hdrs = {"User-Agent": "monitor-bridge", "Content-Type": "application/json"}
    if headers is not None:
        hdrs.update(headers)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=HTTP_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = " ".join(e.read().decode("utf-8", "replace").split())
        except Exception:
            detail = ""
        finally:
            e.close()  # same as _get_json: the body is consumed here, nowhere else
        e.msg = "%s: %s" % (endpoint_label(url), detail[:FETCH_BODY_MAX] or e.msg)
        raise
    except Exception as e:
        raise RuntimeError(describe_fetch_failure(url, e)) from e


def _query_result(body: JsonValue, source: str, what: str = "query") -> list[JsonValue]:
    """The `data.result` list of a Prometheus/Loki response, or `RuntimeError`.

    Both share the {status, data.result} envelope. A non-success status, or a body that is not
    that envelope, raises with `source` ('prometheus'/'loki') in the message; `what` names the
    request ('query', 'labels') in the status message.
    """
    envelope = as_object(body, "%s response" % source)
    if envelope.get("status") != "success":
        raise RuntimeError("%s %s status=%s" % (source, what, envelope.get("status")))
    data = as_object(envelope.get("data", {}), "%s data" % source)
    return as_list(data.get("result", []), "%s result" % source)


def _sample_value(series: JsonObject) -> float:
    """The number in an instant-vector series' `value: [timestamp, "number"]` pair."""
    pair = as_list(series["value"], "series value")
    raw = pair[1]
    if not isinstance(raw, (str, int, float)):
        raise RuntimeError("series value is %s, not a number" % type(raw).__name__)
    return float(raw)


def _labels(series: JsonObject) -> dict[str, str]:
    """A series' `metric` label set; Prometheus and Loki label values are always strings."""
    metric = as_object(series.get("metric", {}), "series metric")
    return {k: v for k, v in metric.items() if isinstance(v, str)}


def _instant_query(
    base_url: str, path: str, query: str, source: str, opener: Opener | None = None
) -> list[JsonObject]:
    """Runs an instant query against `base_url + path` and returns the result list.

    Prometheus and Loki share the same /query?query= shape and {status, data.result}
    envelope. Raises RuntimeError if the endpoint reports a non-success status; `source`
    labels the error ('prometheus'/'loki').
    """
    url = base_url + path + "?" + urllib.parse.urlencode({"query": query})
    return as_object_list(
        _query_result(_get_json(url, opener=opener), source), "%s result" % source
    )


def prom_scalar(
    cfg: Config,
    promql: str,
    base: str | None = None,
    source: str = "prometheus",
    opener: Opener | None = None,
) -> float | None:
    """Run an instant query; return the first result's value as float, or None if empty.

    `base` selects which Prometheus, and every caller in this tree leaves it unset: one
    instance remains, and the `prometheus` gate watches it for every check. The parameter
    exists because it is the seam a second
    Prometheus would be reintroduced through — a caller that passes one also needs a
    reachability gate watching that instance, or it pages beside the gate that already did.
    """
    result = _instant_query(
        base or cfg.PROM_URL, "/api/v1/query", promql, source, opener
    )
    if not result:
        return None
    return _sample_value(result[0])


def prom_vector(
    cfg: Config,
    promql: str,
    base: str | None = None,
    source: str = "prometheus",
    opener: Opener | None = None,
) -> list[tuple[dict[str, str], float]]:
    """Run an instant query; return [(labels: dict, value: float), ...] (empty if none).

    Unlike prom_scalar this keeps each series' labels, so checks can name *which*
    container / target / route is failing.
    """
    return [
        (_labels(series), _sample_value(series))
        for series in _instant_query(
            base or cfg.PROM_URL, "/api/v1/query", promql, source, opener
        )
    ]


def loki_count(
    cfg: Config, selector: str, window: str, opener: Opener | None = None
) -> float | None:
    """Instant LogQL query: total log lines for `selector` over `window`. None if no series.

    Loki's instant-query endpoint evaluates a metric query — here
    sum(count_over_time(SELECTOR[WINDOW])) — and returns a vector with the same
    [ts, value] shape prom_scalar parses, so we read result[0].value[1].
    """
    query = "sum(count_over_time(%s[%s]))" % (selector, window)
    result = _instant_query(cfg.LOKI_URL, "/loki/api/v1/query", query, "loki", opener)
    if not result:
        return None
    return _sample_value(result[0])
