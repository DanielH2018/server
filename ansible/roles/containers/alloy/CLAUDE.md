# alloy — the Pi's log shipper

Grafana Alloy on daniel-pi, shipping this host's container logs and its two health crons'
verdict lines to loki-homelab. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `grafana/alloy:v1.19.2@sha256:…` — pinned by tag AND digest, moved by hand when
  the cluster's Alloy (`roles/k8s/loki-homelab/defaults/main.yml`) moves. Renovate does not
  track `roles/containers/**`.
- **Host:** daniel-pi only (the cluster nodes run the loki-homelab DaemonSet).
- **Port:** 12345 on the Pi's LAN IP — Prometheus scrapes it as `alloy-pi`; everything but
  `/metrics` needs basic auth (`DECIDED:` marker on `ports:` in the compose template).
- **Sources:** container logs through docker-proxy (`job="pi"`), and
  `/var/log/pi-health/*.log` (`job="syslog"`, `machine="daniel-pi"`). The label contract is
  guarded by `ansible/tests/services/test_alloy_pi_config_labels.py`.
- **Config in:** `templates/config.alloy.j2` (River) and `templates/docker-compose.yml.j2`
  (the GOMEMLIMIT/GOGC/GOMAXPROCS sizing, with the measurements behind each value).

## Notable
- **The host journal is not shipped, and RSS is the reason (#1922, measured 2026-09-17).**
  `loki.source.journal` works on this image — the 2026-09-02 spike read 6,573 lines in ten
  minutes — and a `docker.service` unit matcher would ship ~50 lines a day (54 lines for the
  four days to 2026-09-17), so line volume is not the cost. The container's memory is.
  `process_resident_memory_bytes{job="alloy-pi"}` over the 7 days to 2026-09-17: median
  59.3 MB, p95 70.1 MB, and a daily maximum of 75.5–82.1 MB on every one of the seven days,
  against the 96 MiB (100.7 MB) `resources()` cap and a 72 MiB GOMEMLIMIT.
  `go_memstats_sys_bytes` read 96.3 MB at the same time. #1944 settled what that peak is
  (2026-09-18): the top of the GC sawtooth, sampled 1,440 times a day. The live heap is
  38 MB, not the 19 MiB the sizing assumed, so GOGC=50 set a 58 MB heap goal and the Go
  runtime's total reached the 72 MiB GOMEMLIMIT at the top of every cycle; since #1967
  (2026-09-18) GOGC=100 sets a 78 MB goal under an 88 MiB limit and a 128M cap, so the
  peak is ~20 MB higher and the heap is faulted back in half as often. Nothing
  scheduled drives it, and there is no removable cause: what fills the 38 MB is unmeasured
  and pprof is off by decision. The compose template's GOMEMLIMIT and `resources()`
  comments carry the numbers, and `ansible/tests/services/test_alloy_pi_gomemlimit_headroom.py`
  takes its floor from that live heap. The journal reader's own footprint is still
  unmeasured. It is also awkward to measure: sdjournal mmaps the journal files, and here
  those sit on the 128 MB log2ram tmpfs, so the mapped window counts in this process's RSS
  (the metric above) while the physical pages stay journald's — the metric and the cgroup
  cap would disagree, and only a deployed before/after on this host settles it. A reader
  whose cost cannot be measured without a deploy, added to a process whose runtime already
  paces against its memory limit, is the case the issue's own drop rule names. The
  daemon-failure evidence the journal would have carried reaches Loki another way: when
  dockerd stops answering, `pi-recovery-health` (`roles/setup/optimize_pi`) appends the
  newest non-info `journalctl -u docker` lines to its own DOWN record, which the
  `pi_health` source here already ships. To revisit the reader, first make room for it —
  a lower `GOGC` takes ~10 MB off every peak per halving, and the compose template's
  `DECIDED:` marker at the `GOGC` line says what that costs on this host (the heap is
  faulted back from zram once per cycle, so every halving of GOGC doubles the faults) —
  then measure RSS for a week and delete `test_the_journal_is_not_shipped` in the same PR.
  The test is the enforcement for this decision.
- **No healthcheck, on purpose.** The image ships no HTTP client, so a `wget` probe fails to
  EXEC and reads as `unhealthy`; Prometheus `up{job="alloy-pi"}` covers liveness from outside.
  The compose template carries the history.
- **Runs as 1000:1000 with `--storage.path=/data`**, not the image's uid 473 or its
  `/var/lib/alloy`: the compose template says why each half is load-bearing.

## Editing
- Config: `templates/config.alloy.j2` · Compose: `templates/docker-compose.yml.j2`
- Deploy (Pi, driven from daniel-box): `uv run ansible-playbook ansible/deploy.yml --tags "alloy" -e target=daniel-pi`
