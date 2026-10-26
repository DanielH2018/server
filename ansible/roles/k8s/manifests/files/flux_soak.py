"""The soak Job: after revision R rolls out, fail on any restart or alert in the services R changed.

The plan is `docs/target-architecture/deploy/d5-bulk-move.md`, *The soak as a Job (D5.3)*.
The Job runs in `flux-system`, once per promoted revision R, and its exit is the verdict that
notification-controller posts as `flux/soak`. In order, it:

1. checks that the OCIRepository `homelab` holds R, by the origin key C6 names, and takes T0
   from its artifact's `lastUpdateTime`;
2. computes C2's changed set between `last-good`'s `digests.json` and R's, through the one
   `changed_set` function, and exits 0 at once when the set names no service. source-controller
   verifies both artifacts' cosign signatures against the `promote` workflow's identity, so
   this script verifies none. An unverified, unread or malformed file widens the scope to the
   fleet;
3. waits until every Kustomization in the set reads Ready with `lastAppliedOriginRevision`
   naming R (D5 Open question 19), and fails naming each one still pending at the deadline.
   Each poll re-runs step 1's check, so the Job exits 1 once `homelab` stops holding R;
4. waits W past the latest first Ready among the pods R created, then fails on a restart after
   a pod's first Ready, on a pod it cannot map or order, and on a critical or `verify="true"`
   alert activated after T0.

A pass prints the services it judged, then one `event=deploy` logfmt line, the cluster's deploy
annotation. A failure prints the same line with `result=fail` and `duration_s`, writes
`failed-services: <service>,...` to the termination message and exits 1. Every
connection goes through `flux_job_connect.open_url`, which the Job census requires, and the
API server's CA reaches `urlopen` through `SSL_CERT_FILE`, which the Job sets.

Run: python3 flux_soak.py --revision <R> --services services.json ... (the Job's command)
"""

import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
import time
import urllib.error
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from changed_set import changed_set
from flux_job_connect import Request, open_url

API = "https://kubernetes.default.svc"
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
TERMINATION_LOG = "/dev/termination-log"
FLUX_NAMESPACE = "flux-system"
KUSTOMIZATIONS = (
    f"/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/{FLUX_NAMESPACE}/kustomizations"
)
OCIREPOSITORIES = (
    f"/apis/source.toolkit.fluxcd.io/v1/namespaces/{FLUX_NAMESPACE}/ocirepositories"
)
# The OCIRepository that follows `prod`, and the one that follows `last-good`, the base.
SOURCE, BASE_SOURCE = "homelab", "last-good"
# The manifest annotation `flux push artifact --revision` writes, which kustomize-controller's
# getOriginRevision reads. `.status.artifact.revision` is `prod@sha256:<digest>` and names no
# commit, so a check against it never matches R.
ORIGIN_KEY = "org.opencontainers.image.revision"
DIGESTS_FILE = "digests.json"
SOAK = "soak"
# C3's reserved suffixes. A changed-set name with one strips to its tag.
RESERVED_SUFFIXES = ("-deferred", "-snapshot", "-prepull", "-precheck", "-verify")
WORKLOADS = frozenset({"Deployment", "DaemonSet", "StatefulSet"})
UNMAPPED = "unmapped"
# The kube-state-metrics scrape and the rule evaluation that W adds to the longest `for:`.
SCRAPE_AND_EVAL_SECONDS = 120
POLL_SECONDS = 15
# Both alert legs read through the filter M1's Open question 5 sets for C7's gate.
ALERT_FILTER = 'shadow!="true",alertname!="Watchdog"'


class SoakError(Exception):
    """The soak cannot reach a verdict, or a read it must not narrow on failed."""


def origin(revision: str) -> str:
    return f"master@sha1:{revision}"


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


@dataclass(frozen=True)
class Inputs:
    """What the renderer passes the Job.

    Attributes:
        revision: R's commit SHA.
        services: each flux-owned service's longest critical or `verify="true"` `for:`, in
            seconds, and the namespaces its tag renders into.
        stabilise_seconds: `k8s_rollout_stabilise_seconds`, the floor of W.
        scrape_seconds: kube-state-metrics' scrape interval.
        wait_seconds: how long the Kustomization wait runs before it fails.
        own_pod: this Job's pod name, which is never read.
    """

    revision: str
    services: Mapping[str, Mapping]
    stabilise_seconds: int
    scrape_seconds: int
    wait_seconds: int
    own_pod: str = ""


@dataclass(frozen=True)
class Scope:
    kustomizations: frozenset[str]
    services: frozenset[str]
    widened: str = ""


@dataclass
class Verdict:
    """The failures, and on a judged run the services judged and the seconds from T0 to W's end."""

    failures: list[tuple[str, str]] = field(default_factory=list)
    judged: list[str] = field(default_factory=list)
    window: float = 0.0

    def fail(self, service: str, why: str) -> None:
        self.failures.append((service, why))

    def services(self) -> list[str]:
        return sorted({service for service, _ in self.failures})


def _condition(obj: Mapping, kind: str) -> Mapping:
    for condition in (obj.get("status") or {}).get("conditions") or []:
        if condition.get("type") == kind:
            return condition
    return {}


def _true(obj: Mapping, kind: str) -> bool:
    return _condition(obj, kind).get("status") == "True"


def anchor(source: Mapping, revision: str) -> float:
    """T0, once `source`'s artifact is R by its origin key; SoakError otherwise."""
    artifact = (source.get("status") or {}).get("artifact") or {}
    named = (artifact.get("metadata") or {}).get(ORIGIN_KEY)
    if named != origin(revision):
        raise SoakError(
            f"OCIRepository {SOURCE} holds {named!r}, not {origin(revision)}; "
            "an operator reset or a newer promotion moved it"
        )
    return parse_time(artifact["lastUpdateTime"])


def read_digests(reads: Reads, source: Mapping) -> tuple[object, str]:
    """`source`'s verified `digests.json`, or None and why it could not be used."""
    name = source.get("metadata", {}).get("name")
    generation = source.get("metadata", {}).get("generation")
    status = source.get("status") or {}
    artifact = status.get("artifact") or {}
    if not (_true(source, "Ready") and _true(source, "SourceVerified")):
        return None, f"OCIRepository {name} is not Ready with a verified signature"
    if status.get("observedGeneration") != generation:
        return None, f"OCIRepository {name} has not observed its own spec"
    try:
        data = reads.fetch(artifact["url"])
    except (KeyError, SoakError) as err:
        return None, f"OCIRepository {name}'s artifact could not be read: {err}"
    if f"sha256:{hashlib.sha256(data).hexdigest()}" != artifact.get("digest"):
        return None, f"OCIRepository {name}'s artifact does not match its digest"
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            member = next(
                m for m in tar.getmembers() if m.name.lstrip("./") == DIGESTS_FILE
            )
            file = tar.extractfile(member)
            return json.loads(file.read() if file else b""), ""
    except StopIteration, tarfile.TarError, OSError, ValueError:
        return None, f"OCIRepository {name}'s artifact holds no readable {DIGESTS_FILE}"


def scope_of(
    reads: Reads, inputs: Inputs, sources: Mapping[str, Mapping], kustomizations: list
) -> Scope:
    """The Kustomizations and services R changed, widening to the fleet when a file fails."""
    new, new_why = read_digests(reads, sources[SOURCE])
    base = sources.get(BASE_SOURCE)
    if base is None:
        old, old_why = None, f"OCIRepository {BASE_SOURCE} does not exist"
    elif (((base.get("status") or {}).get("artifact") or {}).get("metadata") or {}).get(
        ORIGIN_KEY
    ) == origin(inputs.revision):
        # `settle` moved `last-good` to R before this read, so R against itself would read
        # as an empty set and pass a soak that judged nothing.
        old, old_why = None, f"OCIRepository {BASE_SOURCE} already names R"
    else:
        old, old_why = read_digests(reads, base)
    depends_on = {
        k["metadata"]["name"]: [d["name"] for d in k["spec"].get("dependsOn") or []]
        for k in kustomizations
    }
    suspended = {
        k["metadata"]["name"] for k in kustomizations if k["spec"].get("suspend")
    }
    changed = changed_set(new, old, depends_on, suspended)
    names = changed.names - {SOAK}
    services = {strip(name) for name in names} & set(inputs.services)
    return Scope(frozenset(names), frozenset(services), new_why or old_why)


def strip(name: str) -> str:
    for suffix in RESERVED_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def pending(kustomizations: list, names: Iterable[str], revision: str) -> list[str]:
    """Each of `names` that does not read Ready with its last apply at R."""
    live = {k["metadata"]["name"]: k for k in kustomizations}
    return sorted(
        name
        for name in names
        if not (
            name in live
            and _true(live[name], "Ready")
            and (live[name].get("status") or {}).get("lastAppliedOriginRevision")
            == origin(revision)
        )
    )


def await_ready(
    reads: Reads, inputs: Inputs, names: Iterable[str], deadline: float
) -> None:
    while True:
        left = pending(reads.api(KUSTOMIZATIONS)["items"], names, inputs.revision)
        if not left:
            return
        if reads.clock() >= deadline:
            raise SoakError(
                f"Kustomizations not Ready at R by the deadline: {', '.join(left)}"
            )
        # A reset moves `homelab` off R, and the Kustomizations then apply another revision,
        # so none of them would ever name R. kustomize-controller holds the next promotion's
        # health check behind this Job (#5704), so the Job ends itself rather than wait out
        # its deadline.
        sources = {
            s["metadata"]["name"]: s for s in reads.api(OCIREPOSITORIES)["items"]
        }
        if SOURCE not in sources:
            raise SoakError(f"OCIRepository {SOURCE} does not exist")
        anchor(sources[SOURCE], inputs.revision)
        reads.sleep(POLL_SECONDS)


def _controller(obj: Mapping) -> Mapping:
    for owner in obj.get("metadata", {}).get("ownerReferences") or []:
        if owner.get("controller"):
            return owner
    return {}


def workload_pods(
    reads: Reads, inputs: Inputs, namespaces: Iterable[str], t0: float
) -> dict:
    """Each pod created after T0 whose owner chain ends in a Deployment, DaemonSet or StatefulSet."""
    owners: dict[str, str] = {}
    pods = {}
    for namespace in sorted(namespaces):
        for pod in reads.api(f"/api/v1/namespaces/{namespace}/pods")["items"]:
            meta = pod["metadata"]
            if (
                meta["name"] == inputs.own_pod
                or parse_time(meta["creationTimestamp"]) < t0
            ):
                continue
            owner = _controller(pod)
            kind = owner.get("kind")
            if kind == "ReplicaSet":
                path = (
                    f"/apis/apps/v1/namespaces/{namespace}/replicasets/{owner['name']}"
                )
                if path not in owners:
                    # A refused read exits through SoakError rather than dropping every
                    # Deployment pod from the restart leg.
                    owners[path] = _controller(reads.api(path)).get("kind", "")
                kind = owners[path]
            if kind in WORKLOADS:
                pods[(namespace, meta["name"])] = pod
    return pods


def _series(result: list) -> dict[tuple, list]:
    """Each series' `(namespace, pod[, container])`, mapped to its raw `[time, value]` samples."""
    out: dict[tuple, list] = {}
    for series in result:
        metric = series["metric"]
        key = (metric.get("namespace"), metric.get("pod"))
        if "container" in metric:
            key += (metric["container"],)
        out.setdefault(key, []).extend(series.get("values") or [series.get("value")])
    return out


def wait_seconds(inputs: Inputs, services: Iterable[str]) -> int:
    longest = max((inputs.services[s]["for"] for s in services), default=0)
    return max(inputs.stabilise_seconds, longest) + SCRAPE_AND_EVAL_SECONDS


def _ready_since(pod: Mapping) -> float | None:
    ready = _condition(pod, "Ready")
    if ready.get("status") == "True" and ready.get("lastTransitionTime"):
        return parse_time(ready["lastTransitionTime"])
    return None


def _restarts(pod: Mapping) -> dict[str, int]:
    return {
        c["name"]: c.get("restartCount", 0)
        for c in (pod.get("status") or {}).get("containerStatuses") or []
    }


def judge_pods(
    reads: Reads,
    inputs: Inputs,
    scope: Scope,
    t0: float,
    before: dict,
    verdict: Verdict,
) -> None:
    """The restart legs over every in-scope pod R created, read after the wait."""
    namespaces = _namespaces(inputs, scope)
    pods = workload_pods(reads, inputs, namespaces, t0)
    mapping = _mapping(reads)
    selector = f'namespace=~"{"|".join(sorted(namespaces))}"'
    window = f"[{int(reads.clock() - t0) + 1}s]"
    ready = _series(
        reads.query(f"kube_pod_status_ready_time{{{selector}}}{window}", None)
    )
    scraped = _series(
        reads.query(
            f"kube_pod_container_status_restarts_total{{{selector}}}{window}", None
        )
    )
    gap = 2 * inputs.scrape_seconds
    for key, pod in sorted(pods.items()):
        label = "/".join(key)
        service = mapping.get(key, UNMAPPED)
        if service == UNMAPPED:
            verdict.fail(
                UNMAPPED, f"pod {label} has no homelab:pod_service:info mapping"
            )
            continue
        if service not in scope.services:
            continue
        samples = [float(v) for _, v in ready.get(key, []) if float(v) >= t0]
        first_ready = min(samples, default=None)
        if first_ready is None and _ready_since(pod) is not None:
            verdict.fail(
                service, f"pod {label} is Ready but has no Ready sample since T0"
            )
        counts = before.get(pod["metadata"].get("uid"), {})
        for status in (pod.get("status") or {}).get("containerStatuses") or []:
            name = status["name"]
            if status.get("restartCount", 0) > counts.get(
                name, status.get("restartCount", 0)
            ):
                verdict.fail(
                    service, f"container {label}/{name} restarted during the soak"
                )
                continue
            finished = ((status.get("lastState") or {}).get("terminated") or {}).get(
                "finishedAt"
            )
            if not finished:
                continue
            at = parse_time(finished)
            times = [float(t) for t, _ in scraped.get(key + (name,), [])]
            if not any(abs(t - at) <= gap for t in times):
                verdict.fail(
                    service,
                    f"container {label}/{name} restarted at {finished}, inside a "
                    f"kube-state-metrics scrape gap longer than {gap}s",
                )
            elif first_ready is not None and at >= first_ready:
                verdict.fail(
                    service, f"container {label}/{name} restarted after its first Ready"
                )


def _namespaces(inputs: Inputs, scope: Scope) -> set[str]:
    return {
        ns for s in scope.services for ns in inputs.services[s].get("namespaces", [])
    }


def _mapping(reads: Reads) -> dict[tuple, str]:
    return {
        (s["metric"].get("namespace"), s["metric"].get("pod")): s["metric"].get(
            "service", UNMAPPED
        )
        for s in reads.query("homelab:pod_service:info", None)
    }


def _alert_key(metric: Mapping) -> frozenset:
    # Prometheus's internal labels, the metric name among them, start with two underscores.
    return frozenset(
        (k, v)
        for k, v in metric.items()
        if not k.startswith("__") and k != "alertstate"
    )


def judge_alerts(reads: Reads, scope: Scope, t0: float, verdict: Verdict) -> None:
    """A critical or `verify="true"` alert on an in-scope service, activated after T0."""
    preexisting: set[frozenset] = set()
    if reads.prometheus_start() > t0:
        # A rule with no `for:` re-activates with a fresh `activeAt` when Prometheus restarts.
        # One that was already firing at T0 is not R's doing.
        preexisting = {
            _alert_key(s["metric"])
            for s in reads.query(
                f'last_over_time(ALERTS{{alertstate="firing",{ALERT_FILTER}}}[5m])', t0
            )
        }
    # ALERTS_FOR_STATE holds pending alerts too, and only a firing one fails the soak.
    firing = {
        _alert_key(s["metric"])
        for s in reads.query(f'ALERTS{{alertstate="firing",{ALERT_FILTER}}}', None)
    }
    for series in reads.query(f"ALERTS_FOR_STATE{{{ALERT_FILTER}}}", None):
        metric = series["metric"]
        if (
            _alert_key(metric) not in firing
            or metric.get("shadow") == "true"
            or metric.get("alertname") == "Watchdog"
            or metric.get("service") not in scope.services
            or not (
                metric.get("severity") == "critical" or metric.get("verify") == "true"
            )
            or float(series["value"][1]) < t0
            or _alert_key(metric) in preexisting
        ):
            continue
        verdict.fail(
            metric["service"], f"alert {metric.get('alertname')} fired after T0"
        )


def soak(reads: Reads, inputs: Inputs) -> Verdict:
    """The soak's verdict on R. Raises SoakError when it cannot reach one."""
    start = reads.clock()
    sources = {s["metadata"]["name"]: s for s in reads.api(OCIREPOSITORIES)["items"]}
    if SOURCE not in sources:
        raise SoakError(f"OCIRepository {SOURCE} does not exist")
    t0 = anchor(sources[SOURCE], inputs.revision)
    scope = scope_of(reads, inputs, sources, reads.api(KUSTOMIZATIONS)["items"])
    if scope.widened:
        print(f"soak: scope widened to the fleet: {scope.widened}", file=sys.stderr)
    verdict = Verdict()
    if not scope.services:
        print("soak: R changes no service; nothing to judge")
        return verdict
    await_ready(reads, inputs, scope.kustomizations, start + inputs.wait_seconds)
    namespaces = _namespaces(inputs, scope)
    first = workload_pods(reads, inputs, namespaces, t0)
    before = {pod["metadata"].get("uid"): _restarts(pod) for pod in first.values()}
    mapping = _mapping(reads)
    readies = [
        since
        for key, pod in first.items()
        if mapping.get(key, UNMAPPED) in scope.services | {UNMAPPED}
        and (since := _ready_since(pod)) is not None
    ]
    until = max(readies, default=t0) + wait_seconds(inputs, scope.services)
    if until > reads.clock():
        reads.sleep(until - reads.clock())
    verdict.judged, verdict.window = sorted(scope.services), until - t0
    judge_pods(reads, inputs, scope, t0, before, verdict)
    judge_alerts(reads, scope, t0, verdict)
    return verdict


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
            raise SoakError(f"{what}: HTTP {err.code}") from err
        except OSError as err:
            raise SoakError(f"{what}: {err}") from err

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


def deploy_line(revision: str, services: str, result: str, duration_s=None) -> str:
    """The `event=deploy` line, in `host_lib.deploy_event`'s shape, which this pod cannot import.

    `test_flux_soak.py` holds the two byte-identical; a pass's line is the one before #6164.
    """
    line = f"event=deploy services={services} sha={revision[:8]} result={result}"
    if duration_s is not None:
        line += f" duration_s={int(duration_s)}"
    return f"{line} source=flux"


def main(argv: list[str] | None = None, reads: Reads | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--services", required=True, type=Path)
    parser.add_argument("--stabilise-seconds", required=True, type=int)
    parser.add_argument("--scrape-seconds", required=True, type=int)
    parser.add_argument("--wait-seconds", required=True, type=int)
    parser.add_argument("--prometheus", required=True)
    parser.add_argument("--termination-log", default=TERMINATION_LOG, type=Path)
    args = parser.parse_args(argv)
    inputs = Inputs(
        revision=args.revision,
        services=json.loads(args.services.read_text()),
        stabilise_seconds=args.stabilise_seconds,
        scrape_seconds=args.scrape_seconds,
        wait_seconds=args.wait_seconds,
        own_pod=os.environ.get("HOSTNAME", ""),
    )
    started = time.monotonic()
    try:
        verdict = soak(reads or live_reads(args.prometheus), inputs)
    except SoakError as err:
        print(f"soak: {err}", file=sys.stderr)
        args.termination_log.write_text(f"error: {err}\n")
        print(deploy_line(inputs.revision, "flux", "fail", time.monotonic() - started))
        return 1
    if not verdict.failures:
        if verdict.judged:
            # A judged pass names what it judged, so its log never reads like a missing one (#5419).
            print(
                f"soak: {inputs.revision[:8]} judged {', '.join(verdict.judged)} over "
                f"{verdict.window:.0f}s; pass"
            )
        # The cluster's deploy annotation (D5.3b3): observability's
        # `observability_flux_deploy_annotation_expr` reads this line from Loki.
        print(deploy_line(inputs.revision, "flux", "ok"), flush=True)
        return 0
    for service, why in verdict.failures:
        print(f"soak: {service}: {why}", file=sys.stderr)
    # The failure's annotation names the failed services (#6164).
    duration = time.monotonic() - started
    print(deploy_line(inputs.revision, ",".join(verdict.services()), "fail", duration))
    args.termination_log.write_text(
        f"failed-services: {','.join(verdict.services())}\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
