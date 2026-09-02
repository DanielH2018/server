"""HTTP, PromQL and LogQL fetching for monitor-bridge, and the Kuma push.

Every check body reaches these as `bridge_io.prom_vector(...)`, never by from-import, and the
test suite stubs them HERE — `monkeypatch.setattr(bridge_io, "_get_json", ...)` — where the
callers look them up at call time. A from-import would copy the function into the caller's
globals at import time and the stub would change nothing that runs. The same rule, and the
tests that enforce it, are described in bridge_config.py's header.

The selector builders (`origin_sel`, `cadvisor_sel`, `host_metric_sel`, `_origin_name`) live
here rather than beside the checks because they are the query-building half of fetching, they
read `cfg.PROM_ORIGIN`, and the gates test renders them to prove where the origin pin lands.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import bridge_common
import bridge_config as cfg
from bridge_common import HTTP_TIMEOUT
from bridge_parsing import FETCH_BODY_MAX, describe_fetch_failure, endpoint_label


def origin_sel(*matchers):
    """A `{...}` label-matcher block: the given matchers plus the origin pin, when one applies.

    Returns "" when there is nothing to select on, so `"up%s" % origin_sel()` is a bare `up`
    against the Docker Prometheus and `up{origin="daniel-server"}` against the cluster copy.
    """
    parts = [m for m in matchers if m]
    if cfg.PROM_ORIGIN:
        parts.append(cfg.PROM_ORIGIN)
    return "{%s}" % ", ".join(parts) if parts else ""


def cadvisor_sel(*matchers):
    """A `{...}` block for cAdvisor series, which carry NO origin label — so no origin pin.

    DECIDED: cAdvisor metrics must NOT go through origin_sel(). `origin` is applied by exactly
    one relabel rule, on the `node` job (claude-otel/templates/prometheus.yaml.j2:202); the
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


def host_metric_sel(*matchers):
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
    `origin="daniel-server"` whenever PROM_URL equals CLUSTER_PROM_URL, which the deployed
    env-secret makes true. Pinning these two checks to one host would hide daniel-box's disk
    and memory behind two green tiles, which is precisely the fault HOST_ORIGINS_MIN was added
    for on 2026-08-23. Naming who is OUT keeps every other host in by default.
    """
    parts = [m for m in matchers if m]
    if cfg.HOST_METRIC_ORIGIN_EXCLUDE:
        parts.append('origin!~"%s"' % cfg.HOST_METRIC_ORIGIN_EXCLUDE)
    return "{%s}" % ", ".join(parts) if parts else ""


def _origin_name(labels):
    """The host a per-origin series belongs to, for naming an offender in an alert message.

    The Docker Prometheus has no `origin` label at all (external_labels are applied on
    remote-write, never to local queries), so an empty one means "the only host there is".
    """
    return labels.get("origin") or "host"


# HTTP / parsing helpers (pure-ish, unit-tested)


def _get_json(url, headers=None):
    hdrs = {"User-Agent": "monitor-bridge"}
    if headers is not None:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
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


def _post_json(url, payload, headers=None):
    """POST a JSON body and return the parsed JSON response. Same failure contract as _get_json.

    Only the Cloudflare GraphQL endpoint needs this — every other source here is a GET.
    """
    hdrs = {"User-Agent": "monitor-bridge", "Content-Type": "application/json"}
    if headers is not None:
        hdrs.update(headers)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
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


def _instant_query(base_url, path, query, source):
    """Runs an instant query against `base_url + path` and returns the result list.

    Prometheus and Loki share the same /query?query= shape and {status, data.result}
    envelope. Raises RuntimeError if the endpoint reports a non-success status; `source`
    labels the error ('prometheus'/'loki').
    """
    url = base_url + path + "?" + urllib.parse.urlencode({"query": query})
    data = _get_json(url)
    if data.get("status") != "success":
        raise RuntimeError("%s query status=%s" % (source, data.get("status")))
    return data.get("data", {}).get("result", [])


def prom_scalar(promql, base=None, source="prometheus"):
    """Run an instant query; return the first result's value as float, or None if empty.

    `base` selects which Prometheus. PROM_URL is the default and is what every PROM_DEPENDENT
    check reads; CLUSTER_PROM_URL is what the CLUSTER_DEPENDENT ones read, under a reachability
    gate of their own — see check_k8s_workloads. Since the Docker plane retired (2026-08-14)
    both env vars render to the same cluster Service URL, so the two gates watch one instance;
    the split is kept so a second Prometheus can be reintroduced without moving every caller.
    Pick the base by which gate is meant to watch the check, not by which host answers.
    """
    result = _instant_query(base or cfg.PROM_URL, "/api/v1/query", promql, source)
    if not result:
        return None
    return float(result[0]["value"][1])


def prom_vector(promql, base=None, source="prometheus"):
    """Run an instant query; return [(labels: dict, value: float), ...] (empty if none).

    Unlike prom_scalar this keeps each series' labels, so checks can name *which*
    container / target / route is failing.
    """
    return [
        (series.get("metric", {}), float(series["value"][1]))
        for series in _instant_query(
            base or cfg.PROM_URL, "/api/v1/query", promql, source
        )
    ]


def loki_count(selector, window):
    """Instant LogQL query: total log lines for `selector` over `window`. None if no series.

    Loki's instant-query endpoint evaluates a metric query — here
    sum(count_over_time(SELECTOR[WINDOW])) — and returns a vector with the same
    [ts, value] shape prom_scalar parses, so we read result[0].value[1].
    """
    query = "sum(count_over_time(%s[%s]))" % (selector, window)
    result = _instant_query(cfg.LOKI_URL, "/loki/api/v1/query", query, "loki")
    if not result:
        return None
    return float(result[0]["value"][1])


def loki_vector(query):
    """Instant LogQL query keeping each series' labels — the loki_count peer of prom_vector.

    Not prom_vector(base=LOKI_URL): Loki's instant endpoint is /loki/api/v1/query, and
    prom_vector hardcodes /api/v1/query. Same envelope, different path.
    """
    return [
        (series.get("metric", {}), float(series["value"][1]))
        for series in _instant_query(cfg.LOKI_URL, "/loki/api/v1/query", query, "loki")
    ]


def log_error_counts(selector, pattern, window, by_label="container"):
    """(matches, total) — per-container counts of `pattern`, and the selector's total volume.

    `total` is what keeps this arm honest. The whole arm fails OPEN (see with_log_errors), so a
    selector that matches no stream returns no matches and reads exactly like a healthy estate
    — the trap that shipped HA_BAN_SELECTOR with an `app` label promtail does not emit, and
    pushed "no ip_ban events" through a window containing a real ban. Counting the selector's
    own volume separates "nothing is wrong" from "I asked the wrong question".
    """
    matches = loki_vector(
        "sum by (%s) (count_over_time(%s |~ `%s` [%s]))"
        % (by_label, selector, pattern, window)
    )
    total = loki_count(selector, window)
    return matches, total


def loki_reachable():
    """Is Loki itself reachable and answering queries? (the LOKI_DEPENDENT gate).

    Hits the labels endpoint — a fixed, ingestion-independent query that returns status=success
    whenever Loki is up — so 'Loki is down' (one root cause, one page: Loki Reachable) is separated
    from 'Loki is up but promtail stopped shipping' (Loki Log Ingestion, which still evaluates
    whenever Loki is reachable). Raising -> _evaluate renders the Loki Reachable monitor down.
    """
    data = _get_json(cfg.LOKI_URL + "/loki/api/v1/labels")
    if data.get("status") != "success":
        raise RuntimeError("loki labels status=%s" % data.get("status"))
    return True


def push(token, ok, msg):
    """Pushes an up/down heartbeat plus message to the Kuma push monitor for `token`.

    A no-op, logged, when token is unset. Best-effort: an unreachable Kuma is logged and
    swallowed rather than raised, so it never crashes the check loop.

    Args:
        token: The Kuma push-monitor token; empty/None skips the push.
        ok: Whether the check succeeded (pushed as status "up") or not ("down").
        msg: The status message to attach to the push.
    """
    if not token:
        bridge_common.log("WARN: no push token set; skipping push:", msg)
        return
    qs = urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg})
    try:
        _get_json("%s/api/push/%s?%s" % (cfg.KUMA_URL, token, qs))
    except Exception as e:  # best-effort heartbeat; never crash the loop
        bridge_common.log("push failed (%s):" % msg, e)
