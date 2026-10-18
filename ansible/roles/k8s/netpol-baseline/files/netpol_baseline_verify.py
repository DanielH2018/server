"""netpol-baseline's verify Job: prove the policies are enforced, not merely applied.

`templates/verify-inert/job.yaml.j2` holds the Job, and nothing renders it while Ansible owns
the role (target slice D5, *A verify conversion lands as inert code*). It carries the checks
of the `netpol-baseline-probe` Job and the readiness gate `tasks/main.yml` runs before it.
The pod stands in for a compromised workload: its `app: netpol-baseline-verify` label is on no
caller list, so every `refused` row must refuse it. The Job runs this script twice, once per
stage, which `NETPOL_VERIFY_STAGE` names.

- `gate`, the initContainer. Every active row of `netpol_baseline_probe_targets` must have an
  EndpointSlice endpoint whose `conditions.ready` is `true`. Most legs are inverted, so a
  target that is down would read as fenced. It reads the API server with the pod's
  ServiceAccount token, and the API server's CA reaches `urlopen` through `SSL_CERT_FILE`,
  which only this container sets.
- `legs`, the main container, in the probe's order. The control (`CONTROL_URL`) and the
  sentinel (`SENTINEL_URL`, which admits this pod's `netpol-probe: control` label) must answer.
  The liveness leg (`LIVENESS_URL`) must fetch littlelink through traefik. Each row in
  `TARGETS` must refuse, or answer for `expect: open`. Then pihole must resolve a name in
  `DNS_NAMES`. The liveness and DNS legs run only while the homelab lever is on, so the Job
  leaves their variables empty otherwise.

A leg classifies a failed connect with `flux_job_connect.refused`, as n8n's verify Job does.
A refused leg passes only on a refusal. An HTTP error status, a reset after accept, a read
timeout and a protocol error from a non-HTTP port all mean the target accepted the
connection, so they fail it. A name that does not resolve proves nothing about a policy, so it
fails every leg it reaches.

`NETPOL_VERIFY_DIGEST8` is in the spec only to rename the Job, and the script prints it.
Exit 0 when every check passes, 1 when one fails, naming it.

Run: python3 netpol_baseline_verify.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/netpol-baseline/tests/test_netpol_baseline_verify.py
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from flux_job_connect import Request, open_url, refused

API = "https://kubernetes.default.svc"
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
# The socket timeout of each API read, in seconds.
API_TIMEOUT = 10
# The socket timeout of each leg's connect, in seconds; the probe it replaces used `nc -w 5`.
TIMEOUT = 5
# The liveness fetch's socket timeout, in seconds; the probe used `wget -T 10`.
LIVENESS_TIMEOUT = 10
# How long the control retries a refused connect, in seconds: the helper's own default.
CONTROL_RETRY_SECONDS = 60
# How long the sentinel retries, in seconds: the probe's 12 attempts of up to 10s each. It is
# the one leg whose admission needs kube-router to know this pod's labels, and a pod seconds
# old failed one immediate attempt with the policy, the labels and the target all correct
# (measured 2026-08-20).
SENTINEL_RETRY_SECONDS = 120


class Failed(Exception):
    """A check failed; the message names what is wrong."""


def _close(err: BaseException) -> None:
    """Close the response an `HTTPError` carries, which nothing here reads."""
    if isinstance(err, urllib.error.HTTPError):
        err.close()


def unresolved(err: BaseException) -> bool:
    """Whether `err` means the host name had no address, so nothing was dialled.

    `urlopen` wraps the resolver's error in a `URLError` whose `reason` is a `socket.gaierror`.
    Its `errno` is the resolver's `EAI_*` code, which glibc and musl both define as negative,
    where every errno a connect sets is positive.
    """
    if isinstance(err, urllib.error.HTTPError) or not isinstance(
        err, urllib.error.URLError
    ):
        return False
    code = getattr(err.reason, "errno", None)
    return isinstance(code, int) and code < 0


def _dial(url: str, timeout: float, retry_seconds: float) -> BaseException | None:
    """Open `url` once its connect succeeds or stops retrying; the error, or None on 2xx."""
    try:
        with open_url(url, timeout=timeout, retry_seconds=retry_seconds):
            return None
    except Exception as err:
        _close(err)
        return err


def api_reader(token: str, base: str = API) -> Callable[[str], dict]:
    """A reader that GETs an API path as this pod's ServiceAccount and returns the JSON."""

    def get(path: str) -> dict:
        request = Request(
            base + path,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with open_url(request, timeout=API_TIMEOUT) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as err:
            _close(err)
            raise Failed(f"GET {path} answered HTTP {err.code}") from err
        except (OSError, ValueError) as err:
            raise Failed(f"GET {path} got no usable answer: {err}") from err
        if not isinstance(body, dict):
            raise Failed(f"GET {path} answered with JSON that is not an object")
        return body

    return get


def ready(slices: Mapping) -> bool:
    """Whether any endpoint in an EndpointSliceList states `conditions.ready: true`.

    A Service with no ready pod keeps its EndpointSlice and sets `endpoints` to null, which
    reads as no endpoint here. Only an explicit `true` counts: an absent condition does not.
    """
    return any(
        (endpoint.get("conditions") or {}).get("ready") is True
        for item in slices.get("items") or []
        for endpoint in item.get("endpoints") or []
    )


def gate(rows: Sequence[Mapping], get: Callable[[str], dict]) -> list[str]:
    """Prove every probe target has a ready endpoint, so a refusal is the policy's.

    Args:
        rows: `{service, namespace, plane}` per active row of the target table.
        get: Reads an API path and returns its JSON.

    Raises:
        Failed: A target has no ready endpoint, naming every such target.
    """
    if not rows:
        raise Failed("the target table is empty, so the probe would test nothing")
    down = []
    for row in rows:
        query = urllib.parse.urlencode(
            {"labelSelector": f"kubernetes.io/service-name={row['service']}"}
        )
        path = f"/apis/discovery.k8s.io/v1/namespaces/{row['namespace']}/endpointslices?{query}"
        if not ready(get(path)):
            down.append(f"{row['service']} ({row['plane']})")
    if down:
        raise Failed(
            f"no ready endpoint for {', '.join(down)}, so the probe legs were not run. Most "
            "of them are inverted: a failed connection is the PASS case, so they cannot tell "
            "a fenced target from one where nothing is listening. Fix the readiness first; "
            "the next revision re-runs this Job."
        )
    return [f"gate ok: {len(rows)} probe targets each have a ready endpoint"]


def answers(url: str, retry_seconds: float, failure: str) -> str:
    """Prove `url` accepts this pod's connection; any answer at all passes.

    Raises:
        Failed: The connect was refused or timed out after the retries, or the name did not
            resolve. `failure` opens the message.
    """
    err = _dial(url, TIMEOUT, retry_seconds)
    if err is not None and (refused(err) or unresolved(err)):
        raise Failed(f"{failure} ({url}: {err})")
    return url


def liveness(url: str) -> str:
    """Fetch littlelink through traefik; only a 2xx after redirects passes.

    littlelink has `use_authelia: false`, so the request reaches the app. An Authelia-gated
    app would have forwardauth answer first, and the leg would pass without contacting it.

    Raises:
        Failed: Anything but a 2xx, naming the four causes.
    """
    err = _dial(url, LIVENESS_TIMEOUT, 0)
    if err is None:
        return f"liveness ok: {url} reachable through traefik"
    raise Failed(
        f"LIVENESS FAILED: {url}: {err!r}. Four causes, and the last two look like the "
        "first: 1. littlelink is down; 2. the baseline's traefik rule is wrong; 3. DNS: "
        "Pi-hole answers AAAA for .local names with ::; 4. TLS: this leg verifies the "
        "certificate against the image's CA bundle, where the probe's wget skipped "
        "verification, so an expired or unissued certificate fails it too."
    )


def isolated(row: Mapping) -> str:
    """Prove the connect to `row['url']` is refused or times out.

    Raises:
        Failed: The target accepted the connection, or the connect failed for a reason that
            is no refusal, such as a DNS error.
    """
    url = row["url"]
    err = _dial(url, TIMEOUT, 0)
    if err is not None and refused(err):
        return f"isolated: {url} unreachable from an unlisted caller"
    if err is None or not unresolved(err):
        raise Failed(
            f"POLICY NOT ENFORCED: reached {url} from a pod on no caller list"
            f"{'' if err is None else f' ({err!r})'}. {row['hint']}"
        )
    raise Failed(
        f"no refusal from {url}: {err!r}, which proves nothing about the policy"
    )


def open_port(row: Mapping) -> str:
    """Prove `row['url']` accepts a connection its policy leaves open to every source.

    Raises:
        Failed: The connect was refused, timed out, or the name did not resolve.
    """
    answers(
        row["url"],
        0,
        f"OPEN PORT FAILED: refused a connection {row['policy']} leaves open to every source",
    )
    return f"open port ok: {row['url']} reachable from an unlisted caller, as designed"


def dns(names: list[str], port: int = 53) -> str:
    """Prove pihole resolves one of `names`, which no other resolver can answer.

    The connect after a resolution may succeed or fail; only the resolver's error fails.

    Raises:
        Failed: No name resolved.
    """
    for name in names:
        err = _dial(f"http://{name}:{port}/", TIMEOUT, 0)
        if err is None or not unresolved(err):
            return (
                f"dns ok: pihole answered {name}, so :53 is reachable through the fence"
            )
    raise Failed(
        f"DNS FAILED: pihole did not answer {' or '.join(names)}. Check the pihole policy."
    )


def legs(
    env: Mapping[str, str],
    control_retry_seconds: float = CONTROL_RETRY_SECONDS,
    sentinel_retry_seconds: float = SENTINEL_RETRY_SECONDS,
) -> list[str]:
    """Run every leg in the probe's order, and return one report line each.

    Raises:
        Failed: The first leg that failed.
    """
    lines = [
        "control ok: "
        + answers(
            env["CONTROL_URL"],
            control_retry_seconds,
            "CONTROL FAILED: cannot reach traefik, so nothing below is attributable to the "
            "policy. Check DNS and the traefik Service",
        ),
        "sentinel control ok: "
        + answers(
            env["SENTINEL_URL"],
            sentinel_retry_seconds,
            "SENTINEL CONTROL FAILED: cannot reach speedtest, which the sentinel policy "
            "admits this pod to. Either this pod cannot open connections at all, or admitted "
            "traffic is being denied, so every inverted leg would pass whether or not its "
            "policy works",
        ),
    ]
    if env.get("LIVENESS_URL"):
        lines.append(liveness(env["LIVENESS_URL"]))
    for row in json.loads(env["TARGETS"]):
        lines.append(isolated(row) if row["expect"] == "refused" else open_port(row))
    if names := [n for n in env.get("DNS_NAMES", "").split(",") if n]:
        lines.append(dns(names))
    return lines


def run(env: Mapping[str, str], token_file: str = TOKEN_FILE) -> list[str]:
    """Run the stage `NETPOL_VERIFY_STAGE` names, and return its report lines.

    Raises:
        Failed: The stage is unknown, or one of its checks failed.
    """
    stage = env.get("NETPOL_VERIFY_STAGE")
    head = f"verifying netpol-baseline's render {env.get('NETPOL_VERIFY_DIGEST8', 'unknown')}"
    if stage == "gate":
        get = api_reader(Path(token_file).read_text().strip())
        return [head, *gate(json.loads(env["GATE_TARGETS"]), get)]
    if stage == "legs":
        return [head, *legs(env)]
    raise Failed(f"NETPOL_VERIFY_STAGE is {stage!r}; it must be 'gate' or 'legs'")


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    try:
        lines = run(os.environ if env is None else env)
    except Failed as err:
        print(f"netpol-baseline-verify: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"netpol-baseline-verify: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
