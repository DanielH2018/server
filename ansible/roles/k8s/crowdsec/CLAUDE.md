# crowdsec — WAF / behavioural bouncer in front of Traefik

CrowdSec LAPI plus remote agents; the role registers the agent machines on the LAPI after
applying the manifests.

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
raises no decision, local or CAPI. **`home-ips`** holds the home public IPv4 and IPv6 /64 from
ipify, fed by a root cron on daniel-box. **`remote-ips`** is `files/remote_allowlist.py`, run by
the `crowdsec-jobs` sidecar (below): a client address that got
a 2xx on an `authelia`-gated router, kept 7 days, capped at
`ansible/roles/k8s/crowdsec/files/remote_allowlist.py:CAP` entries, which is what covers a VPN
exit or a phone (#2123). Its module docstring is the design.

To see what is exempt: `… exec deploy/crowdsec -c crowdsec -- cscli allowlists inspect
remote-ips`; to withdraw one, `… cscli allowlists remove remote-ips <ip>`.

**Neither list lifts a ban already in force**, and the remote job cannot see a banned address at
all — a 403 is not a 2xx — so the first burst from a new exit still bans it for up to the cron
period. To lift a ban by hand (a Claude session cannot; the read-only ServiceAccount is refused
`pods/exec`):

```bash
sudo k3s kubectl -n homelab exec deploy/crowdsec -c crowdsec -- cscli decisions delete --ip <ip>
```

## The `crowdsec-jobs` sidecar runs three jobs beside the LAPI

`files/crowdsec_jobs.py` runs in the `crowdsec-jobs` container of the engine pod
(`ansible/roles/k8s/crowdsec/templates/deployment.yaml.j2`) and starts each job on its old host
cron's UTC schedule: the remote allowlist every 5 minutes, the AppSec verify every 15, and the
bouncer prune at minute 17. All three were daniel-box root crons that ran `cscli` through `kubectl
exec` until H2.4b, and `tasks/main.yml` keeps their absent arms. The operator chose the sidecar
over a `pods/exec` grant on 2026-10-18 (#5655).

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
  remote allowlist and AppSec verdicts to their Kuma tiles with URLs from the `crowdsec-jobs`
  Secret. Read the jobs with `probe.py loki-query '{container="crowdsec-jobs"}'`. The prefix is
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
- **The home allowlist is not in the sidecar.** It needs the home IPv6 /64, and the pod network
  has no IPv6, so it stays a daniel-box cron until the operator rules on its form (H2.4c,
  #5701).

## Traps

### A crowdsec deploy races its own rollout
The LAPI-registration task in `ansible/roles/k8s/crowdsec/tasks/main.yml` execs into the running
pod, and `k8s/manifests`' rollout-restart deliberately does not wait. **The `rollout status` gate
ahead of the LAPI tasks is the deliberate exception to the drain's "never wait inline" rule** —
removing it as apparent drift brings the race back (PR #229). `no_log: true` censors the failure to
a bare non-zero return code, so the docs page carries its signature.

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
`ansible/inventory/group_vars/all.yml:crowdsec_k8s_sidecar_agents`, which this role's
registration loop also reads; `crowdsec-node-agents` is hand-written.

Read the job before writing a CrowdSec query: `node` is not a CrowdSec label and only the
DaemonSet job attaches one, so a per-node selector on an engine or sidecar metric matches nothing
(ENFORCED by `ansible/tests/services/test_dashboard_queries_match_this_clusters_labels.py`).

**A sidecar job also needs the pod's containerPort and a :6060 grant**, which netpol-baseline's
`networkpolicy-crowdsec-sidecars.yaml.j2` renders from the same map (#4088). ENFORCED by
`ansible/tests/k8s/test_crowdsec_sidecars_are_scraped.py`.
