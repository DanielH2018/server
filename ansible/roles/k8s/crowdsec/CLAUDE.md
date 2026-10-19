# crowdsec — WAF / behavioural bouncer in front of Traefik

CrowdSec LAPI plus remote agents. The LAPI pod's `lapi-register` initContainer registers the
agent machines and creates the two operator allowlists before the LAPI serves.

This file holds the rules; `docs/crowdsec-waf-record.md` holds the record behind them — the
rollout race, the stalled bouncer pull, the partial-line measurements, the removed Metabase
dashboard and the dashboard findings.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "crowdsec"`
- **Images:** `python` (`crowdsec_k8s_jobs_image`), `alpine/k8s`
  (`crowdsec_k8s_jobs_kubectl_image`), `crowdsecurity/crowdsec` (`crowdsec_k8s_image`)
- **Route:** `crowdsec-lapi.local.<domain>` (LAN only), no Authelia
- **Claim:** `crowdsec-db` (no backup (listed in k3s_longhorn_nobackup_volumes))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — platform — LAPI/AppSec/decision
  engine every bouncer queries; a failed deploy can open or close traffic unpredictably
  fleet-wide. COUPLING NOTE for a future promotion: crowdsec-db also holds LAPI machine
  registrations; a revert past an agent registration leaves that agent's stored password valid
  but the machine unknown
<!-- /generated_from -->

- **Traefik's entry declares `depends_on: [crowdsec]`**, so the LAPI is up before the bouncer
  that needs its credential.
- **`use_authelia: false`** — bouncers authenticate with their own API keys, and `crowdsec-db`
  is on the no-backup tier because decisions expire and re-derive from logs.

## The two operator allowlists, and how to unban yourself

Both are LAPI allowlists (`cscli allowlists`), each fed every 5 min, and an allowlisted address
raises no decision, local or CAPI. **`home-ips`** holds the home public IPv4 address and IPv6
/64, fed in two halves (below). **`remote-ips`** is `files/remote_allowlist.py`, run by
the `crowdsec-jobs` sidecar (below): a client address that got
a 2xx on an `authelia`-gated router, kept 7 days, capped at
`ansible/roles/k8s/crowdsec/files/remote_allowlist.py:CAP` entries, which is what covers a VPN
exit or a phone (#2123). Its module docstring is the design.

To see what is exempt: `… exec deploy/crowdsec -c crowdsec -- cscli allowlists inspect
remote-ips` (or `home-ips`); to withdraw one, `… cscli allowlists remove remote-ips <ip>`. A
`home-ips` entry the job did not write is removed on its next run, because the job owns that
list whole.

**Neither list lifts a ban already in force**, and the remote job cannot see a banned address at
all — a 403 is not a 2xx — so the first burst from a new exit still bans it for up to the cron
period. To lift a ban by hand (a Claude session cannot; the read-only ServiceAccount is refused
`pods/exec`):

```bash
sudo k3s kubectl -n homelab exec deploy/crowdsec -c crowdsec -- cscli decisions delete --ip <ip>
```

## The `crowdsec-jobs` sidecar runs four jobs beside the LAPI

`files/crowdsec_jobs.py` runs in the `crowdsec-jobs` container of the engine pod
(`ansible/roles/k8s/crowdsec/templates/deployment.yaml.j2`) and starts each job on its old host
cron's UTC schedule: the remote allowlist every 5 minutes, the AppSec verify every 15, the
bouncer prune at minute 17, and the home allowlist every 5 minutes from minute 1. All four were
daniel-box root crons that ran `cscli` through `kubectl exec`, until H2.4b for the first three
and H2.4c for the home allowlist. Their absent arms are in
`ansible/roles/setup/k8s_host_files/tasks/crowdsec.yml`. The operator chose
the sidecar over a `pods/exec` grant on 2026-10-18 (#5655).

- **cscli in the sidecar is a copy of the engine's own**, made by the `jobs-cscli` initContainer
  from `crowdsec_k8s_image`. It reads the shared `/etc/crowdsec` (read-only) and `crowdsec-db`, so
  it sees the same database as the LAPI. It is statically linked at the pinned digest; recheck
  with `file` on the next `crowdsec_k8s_image` bump.
- **`crowdsec-db` holds links into the engine image's `/staging`**, which the entrypoint makes
  on every start, and the sidecar's image has no `/staging`. cscli creates its trace directory
  through the `trace` link on every call, so the sidecar mounts an emptyDir at
  `/staging/var/lib/crowdsec/data/trace`. Without it, every job failed with `mkdir
  /var/lib/crowdsec/data/trace: file exists`. The other links name data files no job opens.
- **The scheduler is the jobs' wrapper.** It runs each job as a child process, kills one past its
  timeout, skips a slot whose previous run is still going, and prints `<UTC time> <pod>
  <tag>: status=<up|down> <summary>` to the container log, flushed per line. It pushes the
  remote allowlist, AppSec verify and home allowlist verdicts to their Kuma tiles with URLs
  from the `crowdsec-jobs` Secret. Read the jobs with `probe.py loki-query '{container="crowdsec-jobs"}'`. The prefix is
  the host crons' syslog shape, so monitor-bridge's Swallowed Push Verdicts and `probe.py
  alerts` read this container as they read the host crons.
- **Only the sidecar holds a Kubernetes token.** The pod does not automount one; a projected
  volume at `/var/run/crowdsec-jobs` reaches the sidecar alone, and `kubectl` finds it through
  the kubeconfig in the `crowdsec-jobs` ConfigMap. Its Role reads every homelab pod's log,
  which is why the engine container, which parses attacker-controlled requests, must not
  hold it.
- **A job edit restarts the LAPI.** The ConfigMap is part of the role's render, so the shared
  rollout restart rolls the whole pod.
- **A sidecar crash takes the LAPI out of its Service** until the container restarts, because
  pod Ready needs every container. The scheduler never exits on a job's failure, and its memory
  limit is twice the measured peak (`ansible/roles/k8s/crowdsec/defaults/main.yml:crowdsec_k8s_jobs_mem_limit`).
- **Run a job by hand** (operator-only, the read-only ServiceAccount is refused `pods/exec`):
  `sudo k3s kubectl -n homelab exec deploy/crowdsec -c crowdsec-jobs -- python3
  /jobs/crowdsec_jobs.py --run crowdsec-appsec-verify`. It pushes as a scheduled run does.

## The home allowlist is a host-network lookup and a sidecar sync

The sidecar shares the pod's IPv4-only network, so it cannot see the home IPv6 /64. The
operator split the job in two on 2026-10-18 (#5701):

- **The `crowdsec-home-ips` CronJob looks the addresses up.** It runs
  `files/home_ips_discover.py` every 5 minutes on daniel-box with `hostNetwork: true`. It takes
  the IPv4 address from ipify and the /64 from the host's global addresses in
  `/proc/net/if_inet6`. It writes both, with `looked_up_at`, into the `crowdsec-home-ips`
  ConfigMap. Its ServiceAccount may `patch` that one ConfigMap and nothing else, and it runs no
  `cscli`. `k8s/cronjob-gate` runs it once per deploy.
- **The ConfigMap's `data` has one writer.** The role renders the object with no `data` keys
  (the `DECIDED:` marker in `templates/home-ips-configmap.yaml.j2`), so no apply owns a field
  the CronJob writes. Read it with `kubectl -n homelab get configmap crowdsec-home-ips -o yaml`.
- **The sidecar's `crowdsec-home-allowlist` job syncs the list.** `files/home_allowlist.py`
  reads the ConfigMap and the stored entries (`cscli allowlists inspect home-ips`). For each
  family it adds the wanted value, then removes any other entry of that family.
- **An empty or failed lookup never removes a stored entry.** The CronJob then writes the
  family's `*_error` field and leaves its value alone, so the sidecar keeps the stored /64.
  The IPv4 half still syncs when IPv6 fails (#1248).
- **What the "CrowdSec Home Allowlist" tile means.** DOWN when `looked_up_at` is older than
  three CronJob periods (`crowdsec_k8s_home_ips_stale_after_seconds`), so a dead discovery
  pages. DOWN when a lookup or a cscli call failed. UP with `skipped: WAN unreachable` when
  ipify failed and no WAN probe answered, because the WAN Reachable tile owns that fault
  (#2793).
- **Two distinct global /64s fail the IPv6 lookup** rather than pick one. daniel-server held
  one /64 on 2026-10-18, mixing /64 SLAAC and /128 DHCPv6 addresses inside it. A second /64
  appearing on daniel-box reads as a DOWN naming the count.
- **Read it:** `probe.py loki-query '{container="crowdsec-jobs"} |= "crowdsec-home-allowlist"'`
  for the sync, and the newest `crowdsec-home-ips-*` Job's log for the lookup. The lookup's log
  line names each family's outcome and never an address.

## The Flux verify Job waits in `templates/verify-inert/`

D5's successor to the ban gate in `tasks/verify.yml` is inert code that no render emits. The
manifests role and the renderer's `verify_files` both skip that directory, and crowdsec's flip
PR renames it to `templates/verify/`. The operator confirmed its evidence channel on
2026-10-18 (D5 *Open questions* 9).

- **`files/crowdsec_verify.py` bans daniel-pi through LAPI's machine API**, with no
  `pods/exec`. It sends the same `POST /v1/alerts` that `cscli decisions add` sends, reads it
  back, and lifts it in a `finally`.
- **The edge's answer comes from Prometheus.** The `crowdsec-edge-probe` timer on daniel-pi
  (`ansible/roles/setup/k8s_host_files/tasks/crowdsec.yml`) probes the VIP every minute. The
  Pi's Alloy counts each answer as `loki_process_custom_crowdsec_edge_probe_total` by
  `status`, and Prometheus scrapes it as `alloy-pi`, a path that never crosses Traefik.
- **The gate's hop 2, the bouncer's pull, has no Job form.** LAPI v1.8.1 serves no bouncer
  route to a machine, so a missing 403 names both causes, a pull that never came and an edge
  that does not consult the bouncer.
- **Before the flip:** add the fence peers the template's caller census names, and give the
  spec a digest of the pod's roll inputs. `tests/test_crowdsec_verify.py` holds the red fixtures and renders the
  flipped copy through the Job census.

## Traps

### A crowdsec deploy races its own rollout
The b1-gate in `ansible/roles/k8s/crowdsec/tasks/verify.yml` execs into the running pod, and the
`crowdsec-home-ips` gate run writes the ConfigMap the new pod's sidecar reads, while
`k8s/manifests`' rollout-restart deliberately does not wait. **The `rollout status` gate ahead of
them is the deliberate exception to the drain's "never wait inline" rule.** Removing it as
apparent drift brings the race back (PR #229).

### The `lapi-register` initContainer holds the LAPI down if it fails
Machine registration and the two allowlist creates run in the `lapi-register` initContainer of
`ansible/roles/k8s/crowdsec/templates/deployment.yaml.j2`, against `crowdsec-db` before the LAPI
serves (#5741). cscli writes the SQLite file directly, and
`ansible/roles/k8s/crowdsec/tests/test_jobs_image.py::test_the_lapi_register_init_runs_against_a_fresh_database_with_no_lapi`
runs the rendered script in the pinned image with no network. The script tolerates only an
allowlist that already exists. Any other failure leaves the pod in `Init:Error`, the LAPI out
of its Service for every bouncer, and the deploy's rollout wait red. Read
`kubectl -n homelab logs deploy/crowdsec -c lapi-register` first.

### `--check` skips the whole b1-gate rather than failing in it
You cannot demonstrate a ban is enforced without taking the ban, so
`ansible/roles/k8s/crowdsec/tasks/main.yml` gates its `import_tasks: verify.yml` on
`not k8s_no_mutate`, covering `k8s_dry_run` too. The `when` reaches every task in
`ansible/roles/k8s/crowdsec/tasks/verify.yml`, nested ones included — keep it when adding one.

### The ban gate waits for the edge bouncer's pull, not a timer

The gate proves three hops in order, and its rescue names the hop that failed: the decision reads
back from LAPI; the edge bouncer's newest `last_pull` moves past its value read just after the ban
(every `k8straefik` and `k8straefik@*` row, because the base row never pulls); the edge answers 403
to the Pi. The ban, checks and probe sit in one block with the lift in its `always`, so a failed
gate leaves no ban behind.

**A failure at hop 2 means the plugin's metrics-ticker stall is back, or the traefik pod cannot
reach LAPI** — the LAPI log's `GET /v1/decisions/stream` lines from the traefik pod tell those
apart. `ansible/tests/services/test_crowdsec_ban_gate.py` pins the hop-2 wait, the order and the
lift.

### LAPI adds a bouncer row per Traefik pod IP, and an hourly job prunes them

LAPI creates a `k8straefik@<pod IP>` row per new client IP, and every Traefik restart brings one.
The `crowdsec-jobs` sidecar runs `files/bouncer_prune.py` hourly to clear them. Two rules: **`cscli bouncers delete k8straefik@<ip>` is not the tool** (it exits 0 without
deleting, and deleting the parent takes every row holding the key), and **the module refuses to
prune unless a `k8straefik*` row pulled in the last 50 minutes**, because pruning the last row
makes LAPI answer 403 to the edge. Read a run with
`probe.py loki-query '{container="crowdsec-jobs"} |= "crowdsec-bouncer-prune"'`.

### `UnmarshalJSON : unexpected end of JSON input` is a partial-line read, fixed only upstream
The tailer read a Traefik access-log line before Traefik finished writing it. Each occurrence is
one request the WAF never sees, about 0.05% of traffic. Nothing here fixes it, and the syslog
sources (`ansible/roles/k8s/crowdsec/templates/node-agent-acquis.yaml.j2`) lose lines silently.

**Re-check #2124 on the next `crowdsec_k8s_image` bump**
(set in `ansible/inventory/group_vars/all.yml`): a release carrying
crowdsecurity/crowdsec#4678 closes it. Verify with
`probe.py loki-query '{container="crowdsec-agent"} |= "UnmarshalJSON"' --since 24h` returning
nothing.

## Which Prometheus job covers which agent

Four CrowdSec containers run here and one job in
`roles/k8s/observability/templates/prometheus.yaml.j2` covers each: `crowdsec` (the engine pod, LAPI
plus AppSec), `crowdsec-node-agents` (the DaemonSet), `crowdsec-traefik-agent` and
`crowdsec-authelia-agent` (the two sidecars). The `crowdsec` job renders from the `metrics`
item on this role's `containers_list` entry, which is also where the engine's `:6060` is
defined. The sidecar jobs render one per entry of
`ansible/inventory/group_vars/all.yml:crowdsec_k8s_sidecar_agents`, which the
`lapi-register` initContainer also reads; `crowdsec-node-agents` is hand-written.

Read the job before writing a CrowdSec query: `node` is not a CrowdSec label and only the
DaemonSet job attaches one, so a per-node selector on an engine or sidecar metric matches nothing
(ENFORCED by `ansible/tests/services/test_dashboard_queries_match_this_clusters_labels.py`).

**A sidecar job also needs the pod's containerPort and a :6060 grant**, which netpol-baseline's
`networkpolicy-crowdsec-sidecars.yaml.j2` renders from the same map (#4088). ENFORCED by
`ansible/tests/k8s/test_crowdsec_sidecars_are_scraped.py`.
