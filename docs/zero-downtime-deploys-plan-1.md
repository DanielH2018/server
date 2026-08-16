# Zero-Downtime Deploys — Plan 1: deploy behaviour Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the measurement harness every later slice is graded by, convert the one workload that can roll today, and make the deploy-strategy policy machine-enforced instead of comment-enforced.

**Architecture:** Three self-contained changes to the existing repo, no new platform. A read-only Python probe measures real downtime across a rollout (it never writes to the cluster, so it runs under the `homelab-readonly` service account). A pytest guard renders every k8s manifest via the existing `ansible/tests/_k8s_render.py` and asserts each Deployment's strategy is either `RollingUpdate` or an explicitly justified `Recreate`. `prowlarr-flaresolverr` moves to `RollingUpdate` with an `emptyDir`, retiring its PVC.

**Tech Stack:** Python 3.12 + pytest (`uv run pytest`), Jinja2-templated k8s manifests, Ansible, prek.

**Spec:** `docs/zero-downtime-deploys-design.md`

## Global Constraints

- Ansible is the only write path to the cluster. Plain `kubectl` authenticates as `system:serviceaccount:kube-system:homelab-readonly` (`get list watch` only); `sudo` is denied. Anything that changes cluster state goes through `uv run ansible-playbook`.
- Tests live in a directory already listed in `pyproject.toml` `[tool.pytest.ini_options] testpaths`. `scripts` and `ansible/tests` both are; a new directory is not, and would silently never run.
- Tests must NOT live under `ansible/filter_plugins/` — Ansible's plugin loader imports every `.py` there at deploy time and would choke on the `pytest` import.
- `pytest` runs with `-n auto` (xdist). Tests must be filesystem-read-only and order-independent.
- Manifest templates under `roles/k8s/<role>/templates/` must parse as YAML after rendering — `validate_k8s_manifests.py` enforces this.
- A file dropped from a role's `manifests_files` list stops being staged but its live object survives; retiring an object needs one explicit delete as well.
- Commits are signed. Never use `--no-verify` or `--no-gpg-sign`.
- Run `uv run pytest` (not bare `pytest`) so the pinned env is used.

---

### Task 1: Rollout-gap measurement probe

The acceptance test for every conversion in this program is "requests kept succeeding across a rollout". That needs a tool before it needs a conversion, and the same tool grades slices 2, 4, 5, 6, 7 and 9. It only reads, so it works under the read-only service account.

**Files:**
- Create: `scripts/measure_rollout_gap.py`
- Test: `scripts/test_measure_rollout_gap.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `summarize(samples: list[Sample]) -> GapReport`, where `Sample` is `(monotonic_seconds: float, ok: bool)` and `GapReport` has fields `total: int`, `failures: int`, `longest_gap_s: float`, `gaps: list[tuple[float, float]]`. Task 3 and every later slice invoke the CLI, not the functions.

- [ ] **Step 1: Write the failing test**

Create `scripts/test_measure_rollout_gap.py`:

```python
"""Gap summarisation: the arithmetic that turns poll samples into a downtime verdict.

The polling loop itself is I/O and is not unit-tested; this covers the part that decides
whether a rollout was zero-downtime, which is the part a wrong answer would mislead on.
"""

from __future__ import annotations

from measure_rollout_gap import GapReport, summarize


def test_all_ok_reports_no_gap():
    samples = [(0.0, True), (0.5, True), (1.0, True)]
    assert summarize(samples) == GapReport(total=3, failures=0, longest_gap_s=0.0, gaps=[])


def test_failure_window_runs_from_first_failure_to_recovery():
    samples = [(0.0, True), (0.5, False), (1.0, False), (1.5, True)]
    report = summarize(samples)
    assert report.failures == 2
    assert report.gaps == [(0.5, 1.5)]
    assert report.longest_gap_s == 1.0


def test_two_windows_report_the_longer_one():
    samples = [
        (0.0, True), (1.0, False), (2.0, True),
        (3.0, False), (4.0, False), (5.0, True),
    ]
    report = summarize(samples)
    assert report.gaps == [(1.0, 2.0), (3.0, 5.0)]
    assert report.longest_gap_s == 2.0


def test_trailing_failures_are_measured_to_the_last_sample():
    """A rollout that never recovers must not read as a zero-length gap."""
    samples = [(0.0, True), (1.0, False), (2.0, False)]
    report = summarize(samples)
    assert report.gaps == [(1.0, 2.0)]
    assert report.longest_gap_s == 1.0


def test_leading_failures_are_measured_from_the_first_sample():
    samples = [(0.0, False), (1.0, False), (2.0, True)]
    report = summarize(samples)
    assert report.gaps == [(0.0, 2.0)]


def test_empty_samples_is_not_a_pass():
    """Zero requests is a broken run, not a clean one."""
    report = summarize([])
    assert report.total == 0
    assert report.failures == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest scripts/test_measure_rollout_gap.py -v -n0`
Expected: FAIL — `ModuleNotFoundError: No module named 'measure_rollout_gap'`

- [ ] **Step 3: Write the implementation**

Create `scripts/measure_rollout_gap.py`:

```python
#!/usr/bin/env python3
"""Measure real downtime across a rollout by polling a service while it restarts.

Every zero-downtime claim in docs/zero-downtime-deploys-design.md is graded by this and
not by a manifest: `strategy: RollingUpdate` in a template says what was configured, and
this says what happened. A single-replica RollingUpdate only avoids a gap because maxSurge
rounds up to 1 — that is a scheduler behaviour, not a guarantee, and it fails silently if
the pod cannot be scheduled or the readiness probe is wrong.

Read-only: it issues GETs (or DNS queries) and never touches cluster state, so it runs fine
under the homelab-readonly service account. Trigger the rollout separately — typically
`uv run ansible-playbook ansible/deploy.yml --tags <svc>` in another terminal — while this
is running.

Usage:
    uv run python scripts/measure_rollout_gap.py --url https://grafana.local.example --seconds 180
    uv run python scripts/measure_rollout_gap.py --dns homepage.local.example --server 10.0.0.243 --seconds 180

Exit code is 0 only when zero requests failed, so it is usable as a gate.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

Sample = tuple[float, bool]


@dataclass(frozen=True)
class GapReport:
    total: int
    failures: int
    longest_gap_s: float
    gaps: list[tuple[float, float]] = field(default_factory=list)


def summarize(samples: list[Sample]) -> GapReport:
    """Collapse samples into failure windows.

    A window runs from the first failed sample to the first success after it. A run of
    failures that never recovers is bounded by the last sample instead, so a service that
    stays down reads as a long gap rather than as no gap at all.

    This measures at poll resolution and therefore UNDERSTATES the true gap by up to one
    interval at each end — the service went down somewhere between the last success and the
    first failure. That is the right direction to be wrong in for a pass/fail gate (it never
    invents a gap), but do not quote `longest_gap_s` as an exact outage duration.
    """
    if not samples:
        return GapReport(total=0, failures=0, longest_gap_s=0.0, gaps=[])

    gaps: list[tuple[float, float]] = []
    start: float | None = None

    for ts, ok in samples:
        if not ok and start is None:
            start = ts
        elif ok and start is not None:
            gaps.append((start, ts))
            start = None

    if start is not None:
        gaps.append((start, samples[-1][0]))

    failures = sum(1 for _, ok in samples if not ok)
    longest = max((end - begin for begin, end in gaps), default=0.0)
    return GapReport(total=len(samples), failures=failures, longest_gap_s=longest, gaps=gaps)


def probe_http(url: str, timeout: float, insecure: bool) -> bool:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        # 4xx means something answered — an auth redirect or a 404 is not downtime.
        return exc.code < 500
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError):
        return False


def probe_dns(name: str, server: str, timeout: float) -> bool:
    """Minimal A-record query. Avoids a dnspython dependency for one packet."""
    query = bytearray(b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00")
    for label in name.rstrip(".").split("."):
        query.append(len(label))
        query.extend(label.encode())
    query.extend(b"\x00\x00\x01\x00\x01")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(bytes(query), (server, 53))
        data, _ = sock.recvfrom(512)
        return len(data) > 12 and (data[3] & 0x0F) == 0
    except OSError:
        return False
    finally:
        sock.close()


def run(args: argparse.Namespace) -> GapReport:
    samples: list[Sample] = []
    started = time.monotonic()
    while time.monotonic() - started < args.seconds:
        at = time.monotonic() - started
        if args.dns:
            ok = probe_dns(args.dns, args.server, args.timeout)
        else:
            ok = probe_http(args.url, args.timeout, args.insecure)
        samples.append((at, ok))
        if not ok:
            print(f"  {at:7.2f}s  FAIL", flush=True)
        time.sleep(args.interval)
    return summarize(samples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--url", help="HTTP(S) URL to poll")
    target.add_argument("--dns", help="hostname to resolve instead of polling HTTP")
    parser.add_argument("--server", default="10.0.0.243", help="DNS server for --dns")
    parser.add_argument("--seconds", type=float, default=180.0, help="how long to poll")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between polls")
    parser.add_argument("--timeout", type=float, default=2.0, help="per-request timeout")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    args = parser.parse_args(argv)

    target_desc = args.dns or args.url
    print(f"Polling {target_desc} every {args.interval}s for {args.seconds}s.")
    print("Trigger the rollout now, in another terminal.\n")

    report = run(args)

    print(f"\nrequests   : {report.total}")
    print(f"failures   : {report.failures}")
    print(f"longest gap: {report.longest_gap_s:.2f}s")
    for begin, end in report.gaps:
        print(f"  gap {begin:7.2f}s -> {end:7.2f}s  ({end - begin:.2f}s)")

    if report.total == 0:
        print("\nFAIL: no requests were made.")
        return 2
    if report.failures:
        print(f"\nFAIL: {report.failures} failed requests.")
        return 1
    print("\nPASS: zero failed requests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest scripts/test_measure_rollout_gap.py -v -n0`
Expected: PASS, 6 tests.

- [ ] **Step 5: Verify the CLI parses and rejects a bad invocation**

Run: `uv run python scripts/measure_rollout_gap.py --help`
Expected: usage text, exit 0.

Run: `uv run python scripts/measure_rollout_gap.py --seconds 1`
Expected: exit 2 with `one of the arguments --url --dns is required`.

- [ ] **Step 6: Lint**

Run: `uv run ruff check scripts/measure_rollout_gap.py scripts/test_measure_rollout_gap.py`
Expected: `All checks passed!`

Run: `uv run ruff format --check scripts/measure_rollout_gap.py scripts/test_measure_rollout_gap.py`
Expected: no reformatting needed. If it reports files, run without `--check` and re-run the tests.

- [ ] **Step 7: Commit**

```bash
git add scripts/measure_rollout_gap.py scripts/test_measure_rollout_gap.py
git commit -m "Add a rollout-gap probe to measure downtime instead of asserting it

Every zero-downtime claim in the design is graded by a request loop across a
real rollout, not by reading strategy: RollingUpdate out of a manifest. A
single-replica rolling update only avoids a gap because maxSurge rounds up to
1, which is scheduler behaviour rather than a guarantee — it fails silently if
the pod cannot be scheduled or the readiness probe is wrong.

Read-only, so it runs under the homelab-readonly service account."
```

---

### Task 2: Deploy-strategy guard

41 templates carry a hand-written rationale comment for `strategy: Recreate`. Comments are not enforced: the next service added gets whatever its author copied. This converts the convention into a test, following the allowlist idiom already used by `ansible/tests/test_container_security_context.py` — a new `Recreate` fails until someone adds it with a reason, which is the decision worth forcing.

**Files:**
- Create: `ansible/tests/test_deploy_strategy.py`
- Read (do not modify): `ansible/tests/_k8s_render.py`, `ansible/tests/test_container_security_context.py`

**Interfaces:**
- Consumes: `rendered_docs()` from `ansible/tests/_k8s_render.py`, yielding `(role, template_name, parsed_doc)` for every renderable manifest.
- Produces: nothing later tasks import. Task 3 must remove `("prowlarr", "flaresolverr")` from the `_RECREATE` allowlist in this file.

- [ ] **Step 1: Write the failing test**

Create `ansible/tests/test_deploy_strategy.py`:

```python
"""Every Deployment's update strategy is a decision on the record, not a copied default.

`strategy: Recreate` stops the old pod before starting the new one, so each deploy of that
workload has a hard downtime gap. That is the right call for most of this fleet — sqlite
databases, single-writer TSDBs, a Zigbee radio that accepts one client — and each template
says so in a comment. A comment is not a gate: the next service added inherits whatever its
author copied, and nothing notices.

Two guards:

  * every Deployment is either RollingUpdate or an allowlisted Recreate with a reason. A new
    Recreate fails until it is added below, which forces the author to state why.
  * every ROLLING Deployment reachable through a Service has a readinessProbe. Without one a
    pod is Ready the instant its container starts, so the Service routes to it before it can
    serve — which turns a rolling update into a short outage while looking like the opposite.
    Recreate workloads are exempt: their gap is the point, and a probe does not close it.

Rendering goes through validate_k8s_manifests' own machinery (via _k8s_render), so this
cannot drift from what that validator considers a renderable manifest.
"""

from __future__ import annotations

from _k8s_render import rendered_docs

# (role, deployment name) -> why this workload must stop before it starts.
# Adding an entry is a deliberate act: it means a deploy of this service has a downtime gap.
_RECREATE = {
    # ── sqlite / embedded DB / local config store ──
    ("sonarr", "sonarr"): "sqlite config DB; two instances would double-run import jobs",
    ("radarr", "radarr"): "sqlite config DB; two instances would double-run import jobs",
    ("bazarr", "bazarr"): "sqlite config DB",
    ("prowlarr", "prowlarr"): "sqlite config DB; two instances would double-run RSS syncs",
    ("jellyfin", "jellyfin"): "sqlite library DB",
    ("freshrss", "freshrss"): "sqlite DB plus file-based PHP sessions",
    ("healthchecks", "healthchecks"): "sqlite DB",
    ("speedtest", "speedtest"): "sqlite results DB",
    ("uptime-kuma", "uptime-kuma"): "sqlite DB on two RWO PVCs",
    ("n8n", "n8n"): "sqlite DB under /home/node/.n8n",
    ("home-assistant", "home-assistant"): "sqlite recorder plus singleton device connections",
    ("authelia", "authelia"): "sqlite storage and an in-memory session provider",
    ("crowdsec", "crowdsec"): "sqlite LAPI DB",
    ("claude-otel", "grafana"): "sqlite DB",
    ("scrutiny", "scrutiny-web"): "local config store",
    ("karakeep", "karakeep"): "sqlite DB on the data PVC",
    # ── single-writer TSDB / index ──
    ("claude-otel", "prometheus"): "TSDB holds an exclusive lock on its data directory",
    ("claude-otel", "loki"): "single-writer index on a filesystem store",
    ("claude-otel", "tempo"): "single-writer trace store",
    ("loki-homelab", "loki-homelab"): "single-writer index on a filesystem store",
    ("scrutiny", "scrutiny-influxdb"): "influxdb holds an exclusive lock on its data directory",
    ("karakeep", "karakeep-meilisearch"): "meilisearch holds an exclusive LMDB lock",
    # ── single-writer datastore / world save ──
    ("livesync", "livesync"): "couchdb single-writer data directory",
    ("mosquitto", "mosquitto"): "single-writer persistence DB",
    ("valheim", "valheim"): "two servers writing one world save is worse than the gap",
    ("terraria", "terraria"): "two servers writing one world save is worse than the gap",
    ("tdarr", "tdarr"): "single-writer server DB",
    # ── node-exclusive hardware or network namespace ──
    ("nut", "nut"): "raw USB device plus a loopback hostPort, both node-exclusive",
    ("zigbee2mqtt", "zigbee2mqtt"): "the SLZB coordinator accepts exactly one client",
    ("wg-easy", "wg-easy"): "owns a wireguard interface",
    ("qbittorrent", "qbittorrent"): "VPN killswitch iptables live in the pod netns",
    ("registry", "registry"): "hostPort is node-exclusive",
    # ── in-process state ──
    ("monitor-bridge", "monitor-bridge"): "grace-cycle and hysteresis streaks are in-process",
    ("autofix-bridge", "autofix-bridge"): "candidate-grace streaks are in-process",
    ("janitorr", "janitorr"): "two instances would both walk the library",
    ("valheim-stats", "valheim-stats"): "in-process aggregation state on an RWO PVC",
    ("terraria-stats", "terraria-stats"): "in-process aggregation state on an RWO PVC",
    # ── ingress / DNS topology ──
    ("traefik", "traefik"): (
        "acme.json is RWO and two Traefiks racing to write it corrupt the account "
        "registration; externalTrafficPolicy: Local plus a MetalLB VIP means a second pod "
        "does not receive traffic anyway"
    ),
    ("pihole", "pihole"): "owns the LAN DNS VIP; redundancy is a second instance, not a replica",
    # ── workspace ──
    ("code-server", "code-server"): "live workspace on an RWO PVC",
    # ── stateless, pending conversion ──
    ("prowlarr", "flaresolverr"): "not yet converted; see docs/zero-downtime-deploys-plan-1.md",
}


def _deployments():
    for role, tpl, doc in rendered_docs():
        if doc.get("kind") == "Deployment":
            yield role, tpl, doc


def _services():
    """(role, selector dict) for every Service that selects on labels."""
    for role, _tpl, doc in rendered_docs():
        if doc.get("kind") == "Service":
            selector = doc.get("spec", {}).get("selector")
            if selector:
                yield role, selector


def test_every_recreate_deployment_is_allowlisted_with_a_reason():
    offenders = []
    for role, tpl, doc in _deployments():
        strategy = doc.get("spec", {}).get("strategy", {}).get("type")
        if strategy != "Recreate":
            continue
        key = (role, doc["metadata"]["name"])
        if key not in _RECREATE:
            offenders.append(f"{role}/{tpl} ({doc['metadata']['name']})")

    assert not offenders, (
        "strategy: Recreate means every deploy of these workloads has a downtime gap.\n"
        "Add each to _RECREATE in this file with the reason it cannot roll, or convert it:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_allowlist_has_no_stale_entries():
    """An allowlisted workload that converted must leave the list, or the list stops meaning
    anything."""
    live = {(role, doc["metadata"]["name"]) for role, _tpl, doc in _deployments()}
    recreate = {
        (role, doc["metadata"]["name"])
        for role, _tpl, doc in _deployments()
        if doc.get("spec", {}).get("strategy", {}).get("type") == "Recreate"
    }
    stale = sorted(k for k in _RECREATE if k in live and k not in recreate)
    missing = sorted(k for k in _RECREATE if k not in live)

    assert not stale, f"no longer Recreate — remove from _RECREATE: {stale}"
    assert not missing, f"no such Deployment — remove from _RECREATE: {missing}"


def test_every_reason_is_substantive():
    thin = sorted(k for k, v in _RECREATE.items() if len(v.strip()) < 15)
    assert not thin, f"reason is too thin to be a decision: {thin}"


def test_rolling_deployments_behind_a_service_have_a_readiness_probe():
    """A rolling pod with no readinessProbe is Ready before it can serve, so the Service
    routes to it and the rollout drops requests — the exact failure this program exists to
    prevent."""
    selectors = list(_services())
    offenders = []

    for role, tpl, doc in _deployments():
        spec = doc.get("spec", {})
        if spec.get("strategy", {}).get("type") == "Recreate":
            continue
        labels = spec.get("template", {}).get("metadata", {}).get("labels", {}) or {}
        behind_service = any(
            svc_role == role and all(labels.get(k) == v for k, v in sel.items())
            for svc_role, sel in selectors
        )
        if not behind_service:
            continue
        containers = spec.get("template", {}).get("spec", {}).get("containers", []) or []
        if not any(c.get("readinessProbe") for c in containers):
            offenders.append(f"{role}/{tpl} ({doc['metadata']['name']})")

    assert not offenders, (
        "rolling Deployment behind a Service with no readinessProbe on any container:\n  "
        + "\n  ".join(sorted(offenders))
    )
```

- [ ] **Step 2: Run the test to see the real fleet state**

Run: `uv run pytest ansible/tests/test_deploy_strategy.py -v -n0`
Expected: PASS for all four tests.

If `test_every_recreate_deployment_is_allowlisted_with_a_reason` fails, the failure message lists roles missing from `_RECREATE` — the allowlist above was written from a triage of the tree and a role may have been added since. Add each with a real reason taken from the rationale comment already in its template. Do not add a reason you cannot source from the template or the app's behaviour.

If `test_allowlist_has_no_stale_entries` fails with `no such Deployment`, a name in the allowlist does not match the rendered `metadata.name` — correct the allowlist key to the rendered name, not the role name.

- [ ] **Step 3: Verify the guard actually catches a regression**

Temporarily change `ansible/roles/k8s/littlelink/templates/deployment.yaml.j2` by adding these two lines directly under `spec:`:

```yaml
  strategy:
    type: Recreate
```

Run: `uv run pytest ansible/tests/test_deploy_strategy.py::test_every_recreate_deployment_is_allowlisted_with_a_reason -v -n0`
Expected: FAIL, naming `littlelink/deployment.yaml.j2`.

Revert the edit:

```bash
git checkout ansible/roles/k8s/littlelink/templates/deployment.yaml.j2
```

Run: `uv run pytest ansible/tests/test_deploy_strategy.py -v -n0`
Expected: PASS again.

- [ ] **Step 4: Run the full suite to confirm nothing else broke**

Run: `uv run pytest ansible/tests -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ansible/tests/test_deploy_strategy.py
git commit -m "Enforce the deploy-strategy policy instead of documenting it

41 templates carry a hand-written rationale for strategy: Recreate. A comment
is not a gate — the next service added inherits whatever its author copied,
and nothing notices that a new workload now has a downtime gap on every deploy.

Follows the allowlist idiom already used by test_container_security_context:
a new Recreate fails until someone adds it with a reason, which is the decision
worth forcing. The second guard covers the opposite failure — a rolling pod
with no readinessProbe is Ready before it can serve, so the Service routes to
it mid-rollout and requests drop."
```

---

### Task 3: Convert flaresolverr to a rolling update

`prowlarr-flaresolverr` is a headless-browser captcha solver with no authoritative state. `ansible/roles/k8s/prowlarr/tasks/main.yml` already documents its volume as "a browser profile cache that regenerates on first use, which is also why its class is longhorn-nobackup" — in-repo confirmation that the PVC can go. Replacing it with an `emptyDir` removes the RWO constraint, which is what lets the Deployment roll.

**Files:**
- Modify: `ansible/roles/k8s/prowlarr/templates/deployment-flaresolverr.yaml.j2`
- Delete: `ansible/roles/k8s/prowlarr/templates/pvc-flaresolverr.yaml.j2`
- Modify: `ansible/roles/k8s/prowlarr/tasks/main.yml`
- Modify: `ansible/roles/k8s/prowlarr/defaults/main.yml`
- Modify: `ansible/tests/test_deploy_strategy.py`

**Interfaces:**
- Consumes: `_RECREATE` from Task 2; `scripts/measure_rollout_gap.py` from Task 1.
- Produces: nothing later tasks import.

- [ ] **Step 1: Remove the allowlist entry so the guard fails first**

In `ansible/tests/test_deploy_strategy.py`, delete this entry. `ruff format` wraps it across
four lines because the key plus reason exceeds the 88-column default, so delete all four:

```python
    (
        "prowlarr",
        "flaresolverr",
    ): "not yet converted; see docs/zero-downtime-deploys-plan-1.md",
```

Leave the rest of `_RECREATE` untouched, and do not reflow the other entries.

- [ ] **Step 2: Run the guard to verify it fails**

Run: `uv run pytest ansible/tests/test_deploy_strategy.py -v -n0`
Expected: FAIL on `test_every_recreate_deployment_is_allowlisted_with_a_reason`, naming `prowlarr/deployment-flaresolverr.yaml.j2`.

- [ ] **Step 3: Switch the Deployment to RollingUpdate**

In `ansible/roles/k8s/prowlarr/templates/deployment-flaresolverr.yaml.j2`, replace:

```yaml
  strategy:
    type: Recreate
```

with:

```yaml
  # No authoritative state: /config is a browser profile cache the app rebuilds on first use
  # (see the note in tasks/main.yml), so it moved to an emptyDir and the RWO constraint that
  # forced Recreate is gone. maxUnavailable 0 is what makes this actually zero-downtime — on a
  # single replica it forces the new pod Ready before the old one is removed, where the 25%
  # default would round down to 0 anyway but says nothing about intent.
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
```

- [ ] **Step 4: Replace the PVC volume with an emptyDir**

In the same file, replace:

```yaml
        - name: config
          persistentVolumeClaim:
            claimName: {{ prowlarr_k8s_flaresolverr_claim }}
```

with:

```yaml
        - name: config
          emptyDir: {}
```

- [ ] **Step 5: Verify the manifest still renders**

Run: `uv run python scripts/validate_k8s_manifests.py`
Expected: exit 0, no render or YAML errors.

- [ ] **Step 6: Run the guard to verify it passes**

Run: `uv run pytest ansible/tests/test_deploy_strategy.py -v -n0`
Expected: PASS. `test_rolling_deployments_behind_a_service_have_a_readiness_probe` covers the new rolling Deployment — flaresolverr is behind `service-flaresolverr.yaml` and already has a `readinessProbe`, so this should pass without further change. If it fails, add a readinessProbe rather than exempting it.

- [ ] **Step 7: Stop staging the PVC**

In `ansible/roles/k8s/prowlarr/tasks/main.yml`, remove this line from `manifests_files`:

```yaml
      - pvc-flaresolverr.yaml
```

Delete the template:

```bash
git rm ansible/roles/k8s/prowlarr/templates/pvc-flaresolverr.yaml.j2
```

- [ ] **Step 8: Retire the live PVC**

Dropping the file from `manifests_files` prunes the staged copy but leaves the live object serving — the `manifest-prune-check` cron flags exactly this. Add an explicit removal to `ansible/roles/k8s/prowlarr/tasks/main.yml`, immediately after the `Deploy prowlarr and flaresolverr to the cluster` task:

```yaml
# flaresolverr's config PVC was retired when the Deployment moved to an emptyDir. Dropping
# pvc-flaresolverr.yaml from manifests_files stops it being staged but does not remove the
# live object, so it is deleted here. --ignore-not-found makes this idempotent, which is what
# lets it stay in the role rather than being a one-shot play someone has to remember to run.
- name: Retire the flaresolverr config PVC
  tags: [deploy]
  when: prowlarr_k8s_enabled | bool
  ansible.builtin.command:
    cmd: >-
      k3s kubectl -n {{ k8s_namespace }} delete pvc {{ prowlarr_k8s_flaresolverr_claim }}
      --ignore-not-found --wait=false
  become: true
  changed_when: false
```

Keep `prowlarr_k8s_flaresolverr_claim` in `defaults/main.yml` — this task still references it. Remove the two settings that no longer have a consumer:

```yaml
prowlarr_k8s_flaresolverr_storage_class: longhorn-nobackup
prowlarr_k8s_flaresolverr_size: 1Gi
```

- [ ] **Step 9: Confirm nothing else references the removed variables**

Run: `grep -rn "prowlarr_k8s_flaresolverr_storage_class\|prowlarr_k8s_flaresolverr_size" ansible/`
Expected: no output. If anything matches, restore the variable rather than editing the consumer.

- [ ] **Step 10: Run the full local gate**

Run: `uv run pytest -q`
Expected: all pass.

Run: `prek run --all-files`
Expected: all hooks pass.

- [ ] **Step 11: Commit**

```bash
git add ansible/roles/k8s/prowlarr ansible/tests/test_deploy_strategy.py
git commit -m "Roll flaresolverr instead of recreating it

flaresolverr is a headless-browser captcha solver with no authoritative
state — the role already documents its /config volume as a browser profile
cache that regenerates on first use, which is why it sits on
longhorn-nobackup. The RWO PVC was the only thing forcing Recreate, so it
becomes an emptyDir and the Deployment rolls with maxUnavailable 0.

The live PVC is deleted explicitly: dropping a file from manifests_files
prunes the staged copy but leaves the object serving, which is what the
manifest-prune-check cron exists to catch."
```

- [ ] **Step 12: Deploy**

Run: `./scripts/deploy.sh --tags prowlarr`

Expected: the play completes. Exit code 75 means the git-tree lock stayed busy and **nothing was deployed** — that is not a failure, wait and re-run.

- [ ] **Step 13: Verify the conversion live**

Run: `kubectl -n homelab get deploy flaresolverr -o jsonpath='{.spec.strategy.type}{"\n"}'`
Expected: `RollingUpdate`

Run: `kubectl -n homelab get pvc prowlarr-flaresolverr-config`
Expected: `Error from server (NotFound)`.

- [ ] **Step 14: Measure the rollout with the Task 1 probe**

flaresolverr has no ingress route — it is reachable only in-cluster on port 8191 — so poll it through a port-forward. In one terminal:

```bash
kubectl -n homelab port-forward svc/flaresolverr 8191:8191
```

In a second terminal:

```bash
uv run python scripts/measure_rollout_gap.py --url http://127.0.0.1:8191/ --seconds 180
```

In a third terminal, once the probe says it is polling:

```bash
./scripts/deploy.sh --tags prowlarr
```

Expected: `PASS: zero failed requests`, exit 0.

If the probe reports failures, the conversion is not done. The likely causes, in order: the new pod could not be scheduled (check `kubectl -n homelab describe pod -l app=flaresolverr` for scheduling events); the readinessProbe passes before the browser can serve (raise `initialDelaySeconds`); or the port-forward itself dropped when the old pod died, which is a measurement artefact rather than a service gap — re-run polling an in-cluster client instead before concluding anything.

- [ ] **Step 15: Record the result**

Append the measured result to `docs/zero-downtime-deploys-design.md` under a new `## Measured results` heading at the end of the file:

```markdown
## Measured results

| Slice | Service | Date | Requests | Failures | Longest gap |
|---|---|---|---|---|---|
| 1 | flaresolverr | YYYY-MM-DD | N | 0 | 0.00s |
```

Fill in the real numbers from the probe output. Do not record a result you did not run.

```bash
git add docs/zero-downtime-deploys-design.md
git commit -m "Record the measured flaresolverr rollout result"
```

---

### Task 4: Baseline the holdouts

Approach B in the spec proposes tuning `terminationGracePeriodSeconds`, `minReadySeconds` and image pre-pull across the ~35 remaining `Recreate` workloads. Doing that blind would be a 35-role diff justified by an estimate. The gap's components are image pull, container start, and readiness — the grace period only contributes when an app ignores SIGTERM, and today exactly one template sets it (`valheim`, at 120s). This task measures before it changes anything, so the tuning that follows is aimed at what the data implicates.

**Files:**
- Create: `docs/zero-downtime-baseline.md`
- Modify: `docs/zero-downtime-deploys-design.md`

**Interfaces:**
- Consumes: `scripts/measure_rollout_gap.py` from Task 1.
- Produces: a baseline table later tuning work is measured against.

- [ ] **Step 1: Pick the sample**

Measure five services spanning the blocker classes, chosen because each has an HTTP endpoint the probe can poll and none is load-bearing enough that a deliberate rollout is disruptive:

| Service | URL | Why it is in the sample |
|---|---|---|
| freshrss | `https://freshrss.local.<domain>` | sqlite, typical web app |
| healthchecks | `https://healthchecks.local.<domain>` | sqlite, Django startup |
| speedtest | `https://speedtest.local.<domain>` | sqlite, small app |
| uptime-kuma | `https://uptime.local.<domain>` | sqlite on two RWO PVCs |
| jellyfin | `https://jellyfin.local.<domain>` | large sqlite library DB, slowest expected start |

Resolve the real hostnames first:

Run: `grep -rn "local\." ansible/inventory/group_vars/all.yml | head -5`

Use the actual domain from that output. Note that `-k8s` names do not resolve from a `daniel-server` shell — run the probe from `daniel-box` or from inside a container.

- [ ] **Step 2: Measure each service**

For each service in the table, in one terminal:

```bash
uv run python scripts/measure_rollout_gap.py --url <url> --seconds 240 --interval 0.5
```

and in another, once polling starts:

```bash
./scripts/deploy.sh --tags <service>
```

Record `longest gap` from each run. Expect a non-zero gap for every one of them — these are `Recreate` workloads and a gap is the expected result, not a failure. The number is the point.

- [ ] **Step 3: Write the baseline**

Create `docs/zero-downtime-baseline.md`:

```markdown
# Recreate gap baseline

Measured with `scripts/measure_rollout_gap.py` across a real
`./scripts/deploy.sh --tags <service>` rollout. These are `Recreate` workloads, so a
gap is expected — this records how large it actually is, so that approach B's tuning
can be aimed at what the data implicates rather than applied blind across ~35 roles.

The spec estimated 15–45s from probe configuration. That estimate is superseded by
whatever this table says.

| Service | Date | Longest gap | Notes |
|---|---|---|---|
| freshrss | YYYY-MM-DD | — | |
| healthchecks | YYYY-MM-DD | — | |
| speedtest | YYYY-MM-DD | — | |
| uptime-kuma | YYYY-MM-DD | — | |
| jellyfin | YYYY-MM-DD | — | |

## What the numbers implicate

[Fill in after measuring. The question this answers: is the gap dominated by container
start and readiness, or by something tunable? If every gap is close to the app's own
startup time, `terminationGracePeriodSeconds` tuning buys nothing and approach B should
be narrowed to the services where the numbers say otherwise.]
```

Fill in every row with a measured number. Leave no `—` behind.

- [ ] **Step 4: Update the spec's estimate**

In `docs/zero-downtime-deploys-design.md`, find the line in the *Approach B* section reading:

```
This does not reach zero. It is expected to move an estimated 15–45s window to roughly
5–15s across the holdouts — an estimate to be replaced with measurement in slice 1.
```

Replace the estimate with the measured range and a link to `docs/zero-downtime-baseline.md`.

- [ ] **Step 5: Commit**

```bash
git add docs/zero-downtime-baseline.md docs/zero-downtime-deploys-design.md
git commit -m "Baseline the real Recreate downtime gap before tuning anything

Approach B proposed terminationGracePeriodSeconds and minReadySeconds tuning
across ~35 roles on the strength of a 15-45s estimate read off probe config.
The gap's components are image pull, container start and readiness; the grace
period only contributes when an app ignores SIGTERM, and exactly one template
sets it today. Measuring five services across the blocker classes says which
of those actually dominates, so the tuning can be aimed instead of applied
fleet-wide."
```

---

## What this plan does not cover

Each is a separate plan, for a separate subsystem, written when this one lands:

- **Plan 2 — Pi-hole redundancy** (spec slice 2). A second instance with its own PVC and VIP. Independent of everything here; blocked on open question 4, how clients are handed both VIPs.
- **Plan 3 — the CNPG platform** (spec slice 3). Operator, 2-instance cluster, nightly `pg_dump`.
- **Plan 4 — per-app Postgres migrations** (spec slices 4–9). Gated on Plan 3 and on spec open questions 1–3.

The approach-B tuning itself is deliberately deferred to whatever Task 4 measures, rather than specified here on the strength of an estimate.
