"""HTTP, PromQL and LogQL fetching for monitor-bridge, and the Kuma push.

This is the transport. A check body never calls it: it queries through the `src` argument,
`bridge.sources.Sources`, whose live methods delegate here and whose fake answers in tests, and
`run_once` pushes through `bridge.sources.Sink`, which delegates to `push`. Every function that
sends a request takes an `opener`, the `urllib.request.urlopen`-shaped callable that sends it, so a
transport test hands in a fake opener rather than patching `urllib.request` (#3938). None means
the real `urlopen`, resolved at call time. What the suite still patches on this module is
`_get_json` in the Loki parsing tests. Callers reach it as `bridge.net._get_json`, never by
from-import, because a from-import would copy the function into the caller's globals at import
time and the stub would change nothing that runs. That rule is enforced by
ansible/tests/services/test_bridge_patch_boundary.py.

CONFIGURATION IS A PARAMETER, NOT A GLOBAL. Every helper that reads a URL or the origin pin
takes the frozen `Config` as its FIRST argument, so this module holds no env-derived state and
a test states the configuration it wants by handing one in. `cadvisor_sel`, `_origin_name`,
`_get_json`, `_post_json` and `_instant_query` read no configuration and take none.

The selector builders (`origin_sel`, `cadvisor_sel`, `host_metric_sel`, `_origin_name`) live
here rather than beside the checks because they are the query-building half of fetching, they
read `cfg.PROM_ORIGIN`, and the gates test renders them to prove where the origin pin lands.
"""

from collections.abc import Callable
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import bridge.common

# `cap_push_msg` and `PUSH_MSG_MAX` live in bridge.common so autofix-bridge's push shares them;
# they are re-exported here for the callers and tests that reach them as `net.*`.
from bridge.common import HTTP_TIMEOUT, PUSH_MSG_MAX, cap_push_msg  # noqa: F401
from bridge.config import Config
from bridge.parsing import FETCH_BODY_MAX, describe_fetch_failure, endpoint_label
from bridge.types import JsonObject, JsonValue, as_list, as_object, as_object_list

# `urllib.request.urlopen`'s shape: called with a Request and `timeout=`, it returns a response
# usable as a context manager. Each request function takes one as `opener`; None is the real one.
Opener = Callable[..., Any]


def origin_sel(cfg: Config, *matchers: str) -> str:
    """A `{...}` label-matcher block: the given matchers plus the origin pin, when one applies.

    Returns "" when there is nothing to select on, so `"up%s" % origin_sel(cfg)` is a bare `up`
    against a Prometheus whose series carry no `origin` label, and `up{origin="daniel-server"}`
    against the cluster instance every deployed check reads.
    """
    parts = [m for m in matchers if m]
    if cfg.PROM_ORIGIN:
        parts.append(cfg.PROM_ORIGIN)
    return "{%s}" % ", ".join(parts) if parts else ""


def cadvisor_sel(*matchers: str) -> str:
    """A `{...}` block for cAdvisor series, which carry NO origin label — so no origin pin.

    DECIDED: cAdvisor metrics must NOT go through origin_sel(). `origin` is applied by exactly
    one relabel rule, on the `node` job (observability/templates/prometheus.yaml.j2:202); the
    kubernetes-cadvisor job has none. PromQL does not match an absent label, so an origin-pinned
    cAdvisor query selects the empty vector and every check built on it reports green forever.

    That is not hypothetical — it is what check_restarts, check_oom and check_cpu did from the
    Phase G retarget until 2026-08-24. Live at the time of the fix: the unpinned selector matched
    110 cAdvisor series and the pinned form returned `no data`, while the bridge logged
    "OK restarts / OK oom / OK cpu" off empty vectors on every cycle. OOM kills and sustained CFS
    throttling had no other alert path, so both were unmonitored outright.

    The Docker cAdvisor these checks once shared with the cluster copy retired 2026-08-14, so
    there is no longer a second estate for a pin to disambiguate. Use origin_sel() for series
    that genuinely carry the label — `up`, and the node-exporter families behind check_disk and
    check_mem — and this for anything cAdvisor emits.
    """
    parts = [m for m in matchers if m]
    return "{%s}" % ", ".join(parts) if parts else ""


def host_metric_sel(cfg: Config, *matchers: str) -> str:
    """A `{...}` block for the HOST-level node_* checks, minus origins owned by another check.

    node_* is estate-wide the moment a host runs node-exporter, so check_disk and check_mem
    scan whatever reports. daniel-pi joined that set when its exporter landed — and
    check_pi_pressure already owns Pi disk and memory, with thresholds written for a 456 MB
    box rather than the 90% that suits the two x86 hosts. Without this exclusion the Pi's
    ordinary working state pages twice for one fact, which is exactly the duplication
    check_mem avoids elsewhere by naming check_oom the single source of truth.

    A regex matcher, so HOST_METRIC_ORIGIN_EXCLUDE can carry a `a|b` list. Series with no
    `origin` label at all are KEPT: Prometheus reads a missing label as "", which `!~` on a
    named host does not match.

    DECIDED: an EXCLUDE, never origin_sel(). cadvisor_sel's note points at "the node-exporter
    families behind check_disk and check_mem" as series that genuinely carry `origin`, which
    reads like an invitation to pin them with origin_sel() — do not. PROM_ORIGIN resolves to
    `origin="daniel-server"` by default. Pinning these two checks to one host would hide
    daniel-box's disk and memory behind two green tiles, which is precisely the fault
    HOST_ORIGINS_MIN was added for on 2026-08-23. Naming who is OUT keeps every other host in
    by default.
    """
    parts = [m for m in matchers if m]
    if cfg.HOST_METRIC_ORIGIN_EXCLUDE:
        parts.append('origin!~"%s"' % cfg.HOST_METRIC_ORIGIN_EXCLUDE)
    return "{%s}" % ", ".join(parts) if parts else ""


def _origin_name(labels: dict) -> str:
    """The host a per-origin series belongs to, for naming an offender in an alert message.

    The Docker Prometheus has no `origin` label at all (external_labels are applied on
    remote-write, never to local queries), so an empty one means "the only host there is".
    """
    return labels.get("origin") or "host"


# HTTP / parsing helpers (pure-ish, unit-tested)


def _get_json(
    url: str, headers: dict[str, str] | None = None, opener: Opener | None = None
) -> JsonValue:
    # The explicit User-Agent is REQUIRED, not decoration. Discord sits behind Cloudflare, which
    # 403s the default python-urllib UA with error 1010 — so `check_discord`'s webhook GETs would
    # read as revoked webhooks on every cycle without it. host_lib.discord_post carries the same
    # rationale for the host plane's POSTs; the two programs cannot share a module (monitor-bridge
    # ships only its own files/ into a ConfigMap), so the reason is written in both places rather
    # than in neither.
    hdrs = {"User-Agent": "monitor-bridge"}
    if headers is not None:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=HTTP_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # Re-raise the SAME type: check_discord branches on `e.code`, so wrapping this would
        # silently turn a decisive 404 (webhook revoked) into a generic "unreachable" that
        # rides the retry streak instead of paging.
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            # The consumer reads .code and .msg only; an HTTPError left open holds the
            # response until GC and warns when it gets there.
            e.close()
        # str(HTTPError) already leads with "HTTP Error <code>:", so this contributes the
        # endpoint and the server's own explanation, not the status again.
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
    survives the CLUSTER_PROMETHEUS_URL deletion (#2825) because it is the seam a second
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


def loki_vector(
    cfg: Config, query: str, opener: Opener | None = None
) -> list[tuple[dict[str, str], float]]:
    """Instant LogQL query keeping each series' labels — the loki_count peer of prom_vector.

    Not prom_vector(base=LOKI_URL): Loki's instant endpoint is /loki/api/v1/query, and
    prom_vector hardcodes /api/v1/query. Same envelope, different path.
    """
    return [
        (_labels(series), _sample_value(series))
        for series in _instant_query(
            cfg.LOKI_URL, "/loki/api/v1/query", query, "loki", opener
        )
    ]


def loki_lines(
    cfg: Config, logql: str, window_s: int, limit: int, opener: Opener | None = None
) -> list[tuple[int, str]]:
    """Range LogQL query: the raw lines matching `logql` over the last `window_s` seconds.

    Returns [(loki_ts_ns, line), ...] oldest first. The metric helpers above answer "how
    many"; this one exists for a verdict that needs ORDER — which of a cron's lines is the
    most recent — and no LogQL metric function returns a line's timestamp. A caller that
    receives exactly `limit` lines has hit the cap and holds a truncated window, and has to
    say so rather than decide on the part it got.

    Measured 2026-09-17 against the live Loki, this function, three runs: `{job="syslog"}`
    filtered to the push-outcome lines returned 529 lines over 3h in 0.02s each, so a 5000
    cap is ~9x the population.
    """
    now_ns = time.time_ns()
    params = {
        "query": logql,
        "limit": str(limit),
        "start": str(now_ns - window_s * 1_000_000_000),
        "end": str(now_ns),
        "direction": "forward",
    }
    url = cfg.LOKI_URL + "/loki/api/v1/query_range?" + urllib.parse.urlencode(params)
    streams = as_object_list(
        _query_result(_get_json(url, opener=opener), "loki"), "loki result"
    )
    lines: list[tuple[int, str]] = []
    for stream in streams:
        for entry in as_list(stream.get("values", []), "loki stream values"):
            ts, line = as_list(entry, "loki stream entry")
            if not isinstance(ts, (str, int)) or not isinstance(line, str):
                raise RuntimeError("loki stream entry is not [timestamp, line]")
            lines.append((int(ts), line))
    lines.sort()
    return lines


def push(
    cfg: Config,
    token: str,
    ok: bool,
    msg: str,
    opener: Opener | None = None,
) -> None:
    """Pushes an up/down heartbeat plus message to the Kuma push monitor for `token`.

    A no-op, logged, when token is unset. Best-effort: an unreachable Kuma is logged and
    swallowed rather than raised, so it never crashes the check loop. `msg` is capped at
    `PUSH_MSG_MAX` chars first (`cap_push_msg`), so no check can hand Discord an embed it
    rejects.

    Args:
        cfg: The configuration holding KUMA_URL.
        token: The Kuma push-monitor token; empty/None skips the push.
        ok: Whether the check succeeded (pushed as status "up") or not ("down").
        msg: The status message to attach to the push.
        opener: Sends the GET that carries the push. None is the real `urlopen`.
    """
    if not token:
        bridge.common.log("WARN: no push token set; skipping push:", msg)
        return
    msg = cap_push_msg(msg)
    qs = urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg})
    try:
        _get_json("%s/api/push/%s?%s" % (cfg.KUMA_URL, token, qs), opener=opener)
    except Exception as e:  # best-effort heartbeat; never crash the loop
        bridge.common.log("push failed (%s):" % msg, e)
