# Zero-Downtime Deploys — Plan 3: readiness probes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the three Services that front a workload with no `readinessProbe` from routing traffic to a pod that cannot serve yet, and record the omissions that are deliberate so nobody "fixes" them into an outage.

**Architecture:** Three probes added to existing manifest templates, plus one pytest guard holding an allowlist of the containers that deliberately have none.

**Tech Stack:** k3s, Ansible, Jinja2-templated manifests, pytest.

**Spec:** `docs/archive/zero-downtime/design.md` (slice 1 follow-up)

## This plan is much smaller than the spec implied — read why first

The baseline reported "14 workloads with no `readinessProbe`" and the spec carried that as work. Auditing the rendered manifests container-by-container gives **18 containers across 18 workloads**, and almost none of them want a probe:

| Group | Count | Verdict |
|---|---|---|
| Sidecars where an absent probe is **deliberate** | 4 | Leave. Adding one is actively harmful. |
| Workloads with no Service, but a `livenessProbe` **and** external monitoring | 7 | Leave. Readiness would add rollout gating only. |
| Workloads with no Service and no liveness, but a **Kuma push heartbeat** or a monitor-bridge check | 4 | Leave. Already covered, and better suited than a probe. |
| **Behind a Service, main container, no readiness** | **3** | **Fix. This plan.** |

The evidence for each dismissal, because "already covered" is exactly the claim that should not be taken on trust:

- **The four sidecars are documented as deliberate.** `traefik/templates/deployment.yaml.j2:202` says it outright — *"Deliberately NO readinessProbe: sidecar readiness gates the whole pod's Service"*. `authelia`'s sidecar comment references that same shape, and `crowdsec`'s explains that pod Ready is the AND of all containers so a startupProbe already covers it. Giving Traefik's CrowdSec agent a readinessProbe would mean an agent hiccup takes the **edge proxy** out of service — every route in the homelab, to protect nothing.
- **`cloudflare-ddns` (both Deployments) pushes a Kuma heartbeat** (`KUMA_PUSH_TOKEN` in `deployment-direct.yaml.j2`). For a periodic updater with no server to probe, a stale heartbeat is a *better* signal than readiness: it catches "running but not updating", which a probe cannot see.
- **`scrutiny`, `janitorr`, `autofix-bridge`, `monitor-bridge`, `dri-device-plugin`** all appear in Uptime Kuma static monitors or `monitor-bridge/files/check.py`. `node-exporter` is scraped by Prometheus, so its absence is a down target.

So the real work is three probes. The fourth sidecar — `uptime-kuma`'s `autokuma` — has no written rationale, and Task 3 forces that decision rather than assuming it matches the others.

## Global Constraints

- Ansible is the only write path to the cluster. Plain `kubectl` is a read-only service account; `sudo` is denied.
- Manifest templates under `roles/k8s/<role>/templates/` must parse as YAML after rendering — `scripts/validate/validate_k8s_manifests.py` enforces it.
- Tests live in a directory already in `pyproject.toml` `testpaths`; `ansible/tests` is one. `pytest` runs `-n auto`, so tests must be filesystem-read-only and order-independent.
- A container with **no** `readinessProbe` is Ready the instant it starts. A container **with** one is not Ready until it passes — so a probe that is wrong takes the workload out of service. Wrong probes are worse than absent ones.
- **Pod Ready is the AND of all containers.** Adding a probe to any container gates the whole pod's Service membership.
- Commits are signed. Never `--no-verify` or `--no-gpg-sign`.
- Run `uv run pytest` / `uv run ansible-playbook`, never the bare binaries.

---

### Task 1: TCP readiness for nut and terraria

Both are behind a Service, both speak TCP, and both currently join their Service the moment the container starts — before `upsd` binds, and before Terraria has loaded its world.

**Files:**
- Modify: `ansible/roles/k8s/nut/templates/deployment.yaml.j2`
- Modify: `ansible/roles/k8s/terraria/templates/deployment.yaml.j2`

**Interfaces:**
- Produces: a `readinessProbe` on the `nut` and `terraria` containers, which Task 3's guard then expects.

- [ ] **Step 1: Confirm the ports from the Services, not from memory**

Run: `grep -n -A3 "ports:" ansible/roles/k8s/nut/templates/service.yaml.j2 ansible/roles/k8s/terraria/templates/service.yaml.j2`
Expected: nut `3493`, terraria `7777/TCP`. If either differs, use what the Service says — the probe must target the port the Service routes to.

- [ ] **Step 2: Add nut's probe**

In `ansible/roles/k8s/nut/templates/deployment.yaml.j2`, on the `nut` container beside its existing `livenessProbe`:

```yaml
          # upsd binds 3493 only after it has enumerated the USB UPS, which takes a few
          # seconds. Without this the Service publishes the pod immediately and peanut and
          # Home Assistant's NUT integration get connection-refused on every deploy.
          readinessProbe:
            tcpSocket:
              port: 3493
            initialDelaySeconds: 5
            periodSeconds: 10
            failureThreshold: 3
```

- [ ] **Step 3: Add terraria's probe**

In `ansible/roles/k8s/terraria/templates/deployment.yaml.j2`, on the `terraria` container:

```yaml
          # The world loads before the server accepts connections, so the listener appearing
          # is the readiness signal. Without this the LoadBalancer VIP advertises a server
          # that refuses every join for the length of the load.
          readinessProbe:
            tcpSocket:
              port: 7777
            initialDelaySeconds: 15
            periodSeconds: 10
            failureThreshold: 6
```

`failureThreshold: 6` gives a minute of world loading before the pod is declared unready; terraria measured 21s start→ready in the baseline, so this is headroom, not a tight gate.

- [ ] **Step 4: Verify both render and the probes land on the right containers**

Run: `uv run python scripts/validate/validate_k8s_manifests.py`
Expected: exit 0.

Run: `uv run python -c "import sys; [sys.path.insert(0,p) for p in ('ansible/tests','ansible/filter_plugins','scripts')]; from _k8s_render import rendered_docs; print([(r,c['name'],'readinessProbe' in c) for r,t,d in rendered_docs() if r in ('nut','terraria') and d['kind']=='Deployment' for c in d['spec']['template']['spec']['containers']])"`
Expected: `True` for the `nut` and `terraria` containers.

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/k8s/nut ansible/roles/k8s/terraria
git commit -m "Gate nut and terraria on readiness before their Services publish them

Both sit behind a Service with no readinessProbe, so the pod joins the
endpoints the instant the container starts — before upsd has enumerated the
USB UPS, and before Terraria has loaded its world. Consumers get
connection-refused for the whole startup window on every deploy, which reads
as a flaky service rather than as a deploy artifact."
```

---

### Task 2: Readiness for valheim

Valheim is behind a LoadBalancer VIP and takes the longest to start of the three. **Its ports are UDP** (`service.yaml.j2`: 2456 and 2457, both `protocol: UDP`), so `tcpSocket` cannot be used — Kubernetes probes have no UDP form. This task therefore determines the check empirically rather than assuming one.

**Files:**
- Modify: `ansible/roles/k8s/valheim/templates/deployment.yaml.j2`

- [ ] **Step 1: Find a readiness signal that exists in this image**

Do not invent a probe. Check, in this order, and stop at the first that works:

1. **A status endpoint.** Some Valheim images expose an HTTP status server. Check the role's env for anything like `STATUS_HTTP` / `SUPERVISOR_HTTP` and the image docs:
   `grep -n "env:" -A40 ansible/roles/k8s/valheim/templates/deployment.yaml.j2`
   If one exists and is enabled, use `httpGet` against it.
2. **The listening socket, from inside the pod.** `exec` with `ss -uln | grep -q :2456`, if `ss` or `netstat` is present in the image.
3. **The server process.** `exec` with `pgrep -f valheim_server`, which proves the process is up but *not* that the world has loaded — weaker, and if this is what you use, say so in the comment rather than implying more.

Determine which is available by reading the image's documentation and the role's existing `livenessProbe` (which already had to solve a similar problem — match its mechanism if it is sound).

- [ ] **Step 2: Add the probe you actually verified**

Add a `readinessProbe` to the `valheim` container using the mechanism from Step 1, with `initialDelaySeconds: 30` and `failureThreshold: 20` at `periodSeconds: 15` — Valheim world loads run into minutes, and a gate that expires mid-load would flap the pod out of its Service repeatedly.

Include a comment naming what the check does and does not prove.

- [ ] **Step 3: If no reliable signal exists, stop and say so**

If none of the three options is available in this image, **do not add a guessed probe** — a wrong readinessProbe on a game server behind a VIP takes it permanently out of service, which is worse than the current behaviour. Report `DONE_WITH_CONCERNS`, leave valheim unchanged, and add it to Task 3's allowlist with the reason "UDP-only, no reliable readiness signal in this image".

- [ ] **Step 4: Verify and commit**

Run: `uv run python scripts/validate/validate_k8s_manifests.py`
Expected: exit 0.

```bash
git add ansible/roles/k8s/valheim
git commit -m "Gate valheim on readiness before the VIP advertises it

Valheim's ports are UDP, so tcpSocket is unavailable and the check had to be
determined from the image rather than assumed. A world load runs into minutes,
during which the LoadBalancer VIP currently advertises a server that accepts
nothing."
```

---

### Task 3: Record which omissions are deliberate

Four sidecars have no `readinessProbe` on purpose, and the reasoning lives in three separate template comments. Someone auditing probe coverage — exactly what produced this plan — will find them again and "fix" them, and giving Traefik's CrowdSec sidecar a readinessProbe takes every route in the homelab out of service when the agent hiccups.

**Files:**
- Create: `ansible/tests/test_readiness_coverage.py`

- [ ] **Step 1: Write the guard**

Create `ansible/tests/test_readiness_coverage.py`:

```python
"""Containers with no readinessProbe are a decision, not an oversight.

A container with no readinessProbe is Ready the instant it starts, so a Service publishes it
before it can serve. That is worth fixing where a Service fronts it — and actively harmful for
a sidecar, because pod Ready is the AND of all containers: giving traefik's CrowdSec agent a
readinessProbe would take every route in the homelab out of service whenever the agent
hiccups. `traefik/templates/deployment.yaml.j2` says so in as many words.

So this guard does not demand universal coverage. It demands that every container without a
probe is listed below with the reason, which is the decision worth forcing on whoever adds the
next one.
"""

from __future__ import annotations

from _k8s_render import rendered_docs

_POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet"}

# (role, container) -> why this container has no readinessProbe.
_NO_READINESS = {
    # ── sidecars: pod Ready is the AND of all containers, so a sidecar probe gates the
    #    whole pod's Service. Documented in each template.
    ("traefik", "crowdsec-agent"): "sidecar readiness would gate the edge proxy's Service",
    ("authelia", "crowdsec-agent"): "same shape as traefik's sidecar; would gate SSO",
    ("crowdsec", "metabase"): "startupProbe already gates it; pod Ready is the AND",
    ("uptime-kuma", "autokuma"): "sidecar; readiness would gate uptime-kuma's own Service",
    # ── no Service fronts these; a livenessProbe restarts them and Kuma or Prometheus
    #    notices if they stop working. Readiness would add rollout gating only.
    ("autofix-bridge", "autofix-bridge"): "no Service; liveness plus Kuma monitors",
    ("janitorr", "janitorr"): "no Service; liveness plus Kuma monitors",
    ("karakeep", "time-tagger"): "no Service; liveness probe covers a hung worker",
    ("monitor-bridge", "monitor-bridge"): "no Service; it is itself the monitor, and Kuma "
    "watches it",
    ("n8n", "n8n-runners"): "no Service; task runners are dialled by the broker, not routed",
    ("crowdsec", "crowdsec-agent"): "DaemonSet agent, no Service; liveness covers it",
    ("node-exporter", "node-exporter"): "scraped by Prometheus, so absence is a down target",
    # ── periodic workers with no server to probe. A stale Kuma push heartbeat catches
    #    'running but not doing its job', which readiness cannot see.
    ("cloudflare-ddns", "cloudflare-ddns"): "Kuma push heartbeat; no server to probe",
    ("scrutiny", "collector"): "periodic SMART collection; covered by a Kuma monitor",
    ("dri-device-plugin", "generic-device-plugin"): "registers a kubelet socket; covered by "
    "monitor-bridge's check",
}


def _containers():
    for role, _tpl, doc in rendered_docs():
        if doc.get("kind") not in _POD_KINDS:
            continue
        spec = doc["spec"]["template"]["spec"]
        for container in spec.get("containers", []) or []:
            yield role, doc["metadata"]["name"], container


def test_every_container_without_readiness_is_recorded():
    offenders = []
    for role, workload, container in _containers():
        if "readinessProbe" in container:
            continue
        if (role, container["name"]) not in _NO_READINESS:
            offenders.append(f"{role}/{workload} container {container['name']}")

    assert not offenders, (
        "these containers have no readinessProbe, so a Service publishes them the instant they "
        "start.\nAdd a probe, or record the reason in _NO_READINESS in this file:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_the_record_has_no_stale_entries():
    """A container that gained a probe must leave the list, or the list stops meaning
    anything."""
    without = {
        (role, c["name"]) for role, _w, c in _containers() if "readinessProbe" not in c
    }
    live = {(role, c["name"]) for role, _w, c in _containers()}
    stale = sorted(k for k in _NO_READINESS if k in live and k not in without)
    gone = sorted(k for k in _NO_READINESS if k not in live)

    assert not stale, f"now has a readinessProbe — remove from _NO_READINESS: {stale}"
    assert not gone, f"no such container — remove from _NO_READINESS: {gone}"


def test_every_reason_is_substantive():
    thin = sorted(k for k, v in _NO_READINESS.items() if len(v.strip()) < 20)
    assert not thin, f"reason too thin to be a decision: {thin}"
```

- [ ] **Step 2: Run it and reconcile against reality**

Run: `uv run pytest ansible/tests/test_readiness_coverage.py -v -n0`
Expected: PASS, 3 tests.

The allowlist above was written from an audit of the rendered manifests, and Tasks 1 and 2 remove three entries' worth of containers from the "no probe" set. If `test_the_record_has_no_stale_entries` fails naming `nut`, `terraria` or `valheim`, that is Tasks 1–2 having worked — those keys were never added, so this should not happen; if it does, something else changed and is worth reading before editing.

If `test_every_container_without_readiness_is_recorded` names a container not in the list, **do not invent a reason to silence it.** Find out what covers it — a Kuma monitor, a Prometheus scrape, a liveness probe — and record that. If nothing covers it, that is a finding: add a probe instead.

- [ ] **Step 3: Prove the guard catches a regression**

Remove the `readinessProbe` you added to `nut` in Task 1, run the guard, and confirm it names `nut`. Restore with `git checkout ansible/roles/k8s/nut/templates/deployment.yaml.j2` and confirm it passes again.

- [ ] **Step 4: Full gate and commit**

Run: `uv run pytest -q` and `prek run --all-files`
Expected: both pass.

```bash
git add ansible/tests/test_readiness_coverage.py
git commit -m "Record which containers deliberately have no readinessProbe

The reasoning currently lives in three separate template comments, so the next
probe-coverage audit finds the same four sidecars and 'fixes' them. Pod Ready
is the AND of all containers, so giving traefik's CrowdSec sidecar a
readinessProbe takes every route in the homelab out of service whenever the
agent hiccups.

The guard demands a recorded reason rather than universal coverage, which is
the decision worth forcing on whoever adds the next container."
```

---

### Task 4: Deploy and verify

- [ ] **Step 1: Dry run**

Run: `uv run ansible-playbook ansible/deploy.yml --tags nut,terraria,valheim --check`
Expected: completes, showing the probe additions.

- [ ] **Step 2: Deploy one at a time, checking each before the next**

These are `Recreate` workloads, and a wrong readinessProbe leaves one permanently out of its Service. Do not batch them.

For each of `nut`, `terraria`, then `valheim` (skip valheim if Task 2 reported no reliable signal):

```bash
./scripts/deploy.sh --tags <service>
kubectl -n homelab get pods -l app=<service>          # expect 1/1 or 2/2 Running
kubectl -n homelab get endpoints <service>            # expect a non-empty address list
```

**The endpoints check is the one that matters.** A pod that is `Running` but never Ready reports fine in the pod listing and has *no* endpoints — that is what a wrong probe looks like, and it is the failure this task is watching for.

- [ ] **Step 3: If a workload has no endpoints, revert it rather than tuning in place**

`git revert` the probe commit for that service and redeploy. A game server or the UPS daemon sitting out of its Service while probe timings are guessed at is a real outage; get it serving again first, then work out the correct check.

- [ ] **Step 4: Record the outcome**

Append to `docs/archive/zero-downtime/baseline.md` under *What the numbers implicate*, noting which of the three gained a probe and confirming each still has endpoints after deploy.

```bash
git add docs/archive/zero-downtime/baseline.md
git commit -m "Record the readiness-probe outcome"
```

---

## What this plan does not cover

- **The 15 containers that keep no probe.** Each is recorded in `_NO_READINESS` with what covers it instead. That list is the deliverable, not a backlog.
- **Rollout gating for probe-less workers.** A readinessProbe would make `rollout status` wait for them, catching a deploy that starts a container which then crashloops — the kube-state-metrics failure of 2026-08-07. That is a real gap, but it is a *deploy-verification* concern rather than a traffic-routing one, and it wants solving once for the fleet rather than per workload.
- **The Python 3.14 migration**, which is another session's work. Do not touch `ansible/tests/test_host_scripts_py312.py`.
