# gpu-exporter — GPU utilization per node, read from DRM sysfs

A DaemonSet serving `homelab_gpu_*` on `:9101`, one pod per node. It reads
`/sys/class/drm/card*` and turns what the kernel exposes into series. No Service, no route, no
secret, no volume. See repo-root `CLAUDE.md` for shared conventions.

**Deploy tag:** `--tags "gpu-exporter"`.

## The two nodes have different GPUs, and #1446 named the wrong one

Measured 2026-09-10:

| Node | GPU | Driver | Card |
|---|---|---|---|
| daniel-box | AMD Phoenix3 (`1002:1900`) | `amdgpu` | `card1` |
| daniel-server | Intel TigerLake Iris Xe (`8086:9a49`) | `i915` | `card0` |

The issue asked for an `intel_gpu_top`-based exporter. That would have instrumented
daniel-server — where, on the day it was filed, **no transcoding was happening**. jellyfin and
tdarr both request `devic.es/dri` and both were scheduled on daniel-box, the AMD node. Neither
is pinned, so either can move. Hence a driver-agnostic exporter that follows the pods.

## The utilization series is not the same series on both nodes

This is the one thing to know before writing a panel.

- **amdgpu** exposes `gpu_busy_percent`, a direct 0-100 gauge → `homelab_gpu_busy_percent`.
- **i915 exposes no busy percentage in sysfs at all.** `intel_gpu_top` gets it from the perf
  PMU (`/sys/devices/i915/events/*-busy`), which needs `CAP_PERFMON` and a privileged pod. The
  unprivileged stand-in is `power/rc6_residency_ms`, a monotonic counter of milliseconds spent
  in the RC6 **idle** state → `homelab_gpu_rc6_residency_seconds_total`, a COUNTER whose
  complement is the busy fraction.

So a board covering both nodes plots two expressions, not one:

```promql
homelab_gpu_busy_percent / 100                                  # daniel-box
1 - rate(homelab_gpu_rc6_residency_seconds_total[5m])           # daniel-server
```

`homelab_gpu_frequency_mhz` is normalised to MHz on both — i915 reports MHz at the card,
amdgpu reports Hz through hwmon.

`homelab_gpu_info` is always 1, one series per card, labelled by driver. It exists so that "the
node has a GPU that reports nothing" is distinguishable from "the exporter is not reading
sysfs" — a flat zero looks identical to a broken read path otherwise.

## Notable

- **Unprivileged**, unlike the `dri-device-plugin` DaemonSet it complements. Every file read is
  `-r--r--r--`, so the pod runs as 65534 with a read-only `/host/sys` and no capability.
- **Cards are discovered, never indexed.** The numbers differ per node and are not stable —
  daniel-box's `card1` node was created at the 2026-09-09 20:10 reboot.
- **`card[0-9]*` also matches connectors** (`card1-DP-1`, `card1-Writeback-1`). They are told
  apart by having no `device/driver` link, and `Path.resolve()` on a missing path returns it
  rather than raising — so the existence check comes first. Getting that wrong reported nine
  connectors as GPUs with the driver named `"driver"`; `test_discovery_rejects_connectors_that_match_the_card_glob`
  is the guard.
- **No Service, deliberately.** A ClusterIP in front of a 2-pod DaemonSet round-robins, so one
  scrape target would give one series flipping between two different GPUs. The `gpu` job uses
  pod discovery and labels each target with `origin: <node>`, like the `node` job.
- **The script rides in a ConfigMap the manifests carry**, so the shared rollout-restart in
  `roles/k8s/manifests` covers an edit to it. That is why this role needs no `checksum/`
  pod annotation of its own.

## Editing

- Exporter: `files/gpu_exporter.py`. Tests: `tests/` (registered in `pyproject.toml`
  `testpaths`). `uv run python files/gpu_exporter.py --once --sysfs-root /sys` prints one
  exposition against the local node without serving.
- Manifests: `templates/{configmap,daemonset}.yaml.j2`. Scrape job: `job_name: gpu` in
  `roles/k8s/claude-otel/templates/prometheus.yaml.j2`.
