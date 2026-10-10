# CrowdSec record — the incidents, the measurements and the sidecar agent seeding

Working-out moved off `ansible/roles/k8s/crowdsec/CLAUDE.md` (#2991), which a session reads on
every touch of the WAF. The role doc keeps the rules; this page keeps the incident each rule was
written after, the measurements behind the numbers, how the traefik and authelia sidecar agents are
seeded, and what the removed Metabase dashboard showed.

## The rollout race the `rollout status` gate closed

The `rollout status` gate ahead of the LAPI tasks (PR #229, 2026-08-16) closed a race between the
"Register the remote agent machines on the LAPI" task and the manifests' rollout-restart, which
deliberately does not wait. `no_log: true` on that task censors the error body, so a surviving
failure reads as an opaque "Module failed: non-zero return code" and looks like a credential or RBAC
problem. A `kubectl wait --for=condition=Available` gate would not have helped, because the pod is
single-replica and the old pod satisfies the condition.

If the signature returns, wait for `kubectl -n homelab get pods` to show `crowdsec` at `1/1`, then
re-run. The second pass rolls nothing and succeeds.

## The parser whitelist the remote allowlist supersedes

`crowdsec-trusted-remote-whitelist.yaml` is a parser whitelist of pinned /32s, written before
`remote-ips` existed. It is kept as the fallback: with the cron live its entries are redundant, not
wrong, so leaving it in place costs nothing and removing it would drop the only exemption that
survives a broken cron.

## Why the ban gate waits on a pull rather than a timer

Until #2752 the gate probed on a fixed 80s timer. On 2026-09-27 that failed a 58-service deploy
with eight 302s, then passed on a re-run. The decision had landed at LAPI at 12:25:18Z, but the
Traefik bouncer made no stream pull from 12:22:24Z to 12:32:24Z. That was the plugin's
metrics-ticker stall, which `metricsUpdateIntervalSeconds: 0` turns off; the `DECIDED: no
usage-metrics ticker` comment in `ansible/roles/k8s/traefik/templates/dynamic.yaml.j2` has the
evidence.

LAPI records bouncer pulls on an auto-created `k8straefik@<pod IP>` row, one per Traefik pod IP.
The base `k8straefik` row never pulls, so the gate reads every `k8straefik` and `k8straefik@*`
row. Reading only the base row failed the gate's first live run, after the pull it waited for had
already happened.

Before the check-mode guard, check mode ran the ban probe against an un-banned host and burned
all eight retries on every `--check` of this role. A dry run that always reports red trains an
operator to skip the check, which is exactly what `.claude/rules/ansible.md` asks for before
touching production state.

## How the bouncer rows accumulate, and what the prune cannot do

LAPI authenticates a bouncer by its API-key hash and its client IP. When a known key arrives from
an IP with no row, LAPI creates `k8straefik@<ip>` and never touches the old row again. Each
Traefik restart brings a new pod IP, and by 2026-09-27 that had left 80 rows, each a valid
identity for the one key (#2762).

`crowdsec-prune-bouncers.sh` runs `files/bouncer_prune.py` hourly as root on daniel-box, and the
module runs `cscli bouncers prune -d 60m --force`. That includes the base `k8straefik` row, which
the image entrypoint re-adds from `BOUNCER_KEY_k8straefik` at the next engine start.

- `cscli bouncers delete k8straefik@<ip>` cannot do this. It exits 0 without deleting an
  auto-created row, and deleting the parent deletes every row that holds the key.
- Pruning the live row is harmless while another row holds the key: the next pull re-creates it
  and gets a full resync.
- Pruning the last row that holds the key makes LAPI answer 403 to the edge, which is why the
  module refuses to prune unless a `k8straefik*` row pulled in the last 50 minutes.

Each run logs one line under the `crowdsec-bouncer-prune` syslog tag, taken from a second `cscli
bouncers list` after the prune. It names the rows deleted, the count of `k8straefik` rows left and
the oldest `last_pull` among them; a refusal starts `status=down`. There is no Kuma monitor,
because a failed run only lets rows accumulate until the next run succeeds. To stop pruning,
remove the "Schedule the bouncer prune" task and set its cron `state: absent` once — a hand delete
of `/etc/cron.d/crowdsec-bouncer-prune` lasts only until the next `crowdsec` deploy.

## The partial-line read behind `UnmarshalJSON : unexpected end of JSON input`

The agent sidecar in the traefik pod logs this against a Traefik access-log line cut at a random
offset, 150 to 900-plus bytes in and never at a fixed ceiling. It is not the acquisition buffer
and not the rotate sidecar: the agent tails through `nxadm/tail` without `CompleteLines`, so when
the tailer reaches EOF in the middle of a line Traefik is still writing, the library emits the
partial line, then seeks to the end of the file. The partial line fails the traefik parser with
this error. The remainder arrives as a second line that does not start with `{` and is dropped
silently, or is skipped by the seek.

Each occurrence is one request the WAF never sees, and the error count is a lower bound —
upstream's second data point measured 389 lines lost against 168 errors. Measured 2026-09-21 over
24h: 94,043 requests served, 51 errors, 59 parser failures, about 0.05% of the edge's traffic. The
same tailer reads `authelia.log` and both nodes' `auth.log`, and a partial syslog line fails the
parser with no error at all, so nothing here can count that loss.

Nothing in this repo fixes it. `CompleteLines` is not an `acquis.yaml` key, Traefik's
`bufferingSize` batches entries into a channel and still writes one line per call, and
`poll_without_inotify` only changes when the reader wakes. The fix is
`crowdsecurity/crowdsec#4678`, which sets `CompleteLines: true`; on 2026-09-22 it was open and in no
release. Building a patched image through `k8s/image-builder` was rejected: it would take the WAF
binary out of Renovate's view for a 0.05% loss.

## The Metabase dashboard was removed (2026-08-22)

**HISTORY — the Metabase sidecar.** The engine pod carried a Metabase sidecar, a `metabase-seed` initContainer and an Authelia-gated `crowdsec.local.<domain>` route until 2026-08-22. Its startupProbe withheld the `crowdsec` Service endpoints that front LAPI and AppSec (observed 2026-08-16: LAPI serving while the pod sat 1/2 with no endpoints), and the four Security-folder Grafana boards already covered its aggregate view.

**What was lost has no Grafana equivalent.** Metabase read the LAPI's `decisions` and `alerts` tables
directly, so it showed what Prometheus has no label for: per-IP identity, geography, ASN, decision
origin and row-level tables. Loki is not a substitute, because the engine's alert insertions never
reach pod stdout (verified over 7 days against a DB holding 424 alerts younger than that). Use
`cscli decisions list` and `cscli alerts list` for per-ban detail. Upstream
`crowdsecurity/grafana-dashboards` is Prometheus-only and ships the same panel set this repo
already carries, so it cannot fill the gap either.

## Sidecar agent seeding

The traefik pod and the authelia pod each run a `crowdsec-agent` sidecar and three seeding init
containers, in the order `crowdsec-hub-install`, `crowdsec-config-install`, `crowdsec-data-install`.
Both pods render them from `ansible/templates/crowdsec-agent.yml.j2`, so the order and the
tolerances hold for both, and a change to one is a change to both (#3741). Authelia's pod adds
`wait-for-redis` after these three, which `docs/authelia-sessions-and-crowdsec-init.md` covers.

The image entrypoint opens with a "Populating configuration directory" step, an
`rsync -a --ignore-existing /staging/etc/crowdsec/* /etc/crowdsec` that runs under `set -e` and only
while `/etc/crowdsec/config.yaml` is absent. About twenty staged files are root-only: the LAPI and
online credentials and the bundled hub tree. The non-root sidecar therefore exits 23 on that rsync,
and the kubelet restarts it. The restart finds `config.yaml`, skips the block, and the pod settles
at 2/2 Running with one restart on the clock. That restart failed every authelia deploy's health
gate, because `probe.py health` fails closed on any container restart inside its 180s window
(#1173; traefik hit it first as #976). Each init container below works around one half of that
staging behaviour.

1. **`crowdsec-hub-install` runs first, as root with `CHOWN` and `DAC_READ_SEARCH`.** The rsync
   skips the staged hub tree, which is part of the exit 23 it tolerates, but it copies the parser
   configs, which are symlinks into that tree. `geoip-enrich.yaml` then resolved to a hub file that
   was never staged, and the agent dropped the parser once per parser-load pass with `Ignoring file …
   lstat /etc/crowdsec/hub/…: no such file or directory`. GeoIP enrichment stayed dead behind a
   healthy pod (#1211). This container copies the tree with `cp -a`, hands it to the pod uid with
   `chown -R`, and runs `chmod -R a+rX,u+w`. The agent's entrypoint writes into the tree on every
   start, so a root-owned copy fails with `permission denied` and exits the sidecar. The container
   ends in `exit 0`.
2. **`crowdsec-config-install` runs as the pod uid, never as root.** It runs the entrypoint's rsync
   itself so the sidecar finds `config.yaml` already present, then installs `acquis.yaml` and the
   whitelist parsers over the staged copies, and creates the log file the app must write. Root with
   `ALL` dropped cannot `chown`, so owning the file by creating it as the pod uid sidesteps the
   capability.
3. **`crowdsec-data-install` runs as root with `DAC_READ_SEARCH`.** The image ships its datafiles at
   `/staging/var/lib/crowdsec/data` mode 0600 root:root, and the entrypoint symlinks them into the
   data volume instead of copying. The non-root agent cannot read through the link, so GeoIP never
   initialises (`unable to open GeoLite2-City.mmdb: permission denied`) behind a 2/2 Running pod
   (#1177; traefik hit it first as #990). This container copies the files in world-readable, which
   defeats the symlink because the entrypoint's `[ ! -e ]` guard skips a name that already exists.
   It copies per file and ends in `exit 0`, so an unreadable file leaves that one name on the
   symlink path.

Hub-install and data-install are the pod's two `runAsUser: 0` containers. Each ends in `exit 0`
because both host pods roll under `Recreate`: the old pod is already gone, so a failing init
container takes the edge or SSO down for the whole homelab (the #986 outage) instead of costing one
parser.

**Hub-install must run before the rsync, or it no-ops silently.** The rsync recurses from the parent
listing, so it creates `/etc/crowdsec/hub` owned by the pod uid before it fails to read into it.
Root with `ALL` dropped cannot write into another uid's directory, because `DAC_READ_SEARCH` is
the read half of the override and `DAC_OVERRIDE` the write half. The copy would fail, `exit 0` would
swallow it, and the pod would come up with the warning intact. Going first, hub-install creates the
directory while the emptyDir is still empty, and the rsync's `--ignore-existing` then leaves those
files alone.

**Exit 23 is the only rsync status config-install tolerates.** A blanket `|| true` would start an
agent on a half-populated config with nothing saying so. The files exit 23 skips are the LAPI and
online credentials, which the agent regenerates on start. The `DECIDED:` marker on that tolerance in
the template was amended by #1211, not reversed: the skipped hub tree is not all re-downloaded by
`cscli hub update` before the parser load, which is why hub-install exists.

Three tests hold both pods to this:
`ansible/tests/services/test_crowdsec_config_install_seeds_staged_tree.py`,
`ansible/tests/services/test_crowdsec_optional.py` and
`ansible/tests/services/test_crowdsec_hub_install_stages_the_hub_tree.py`.
To verify a deploy of either host role, read the sidecar's restart count rather than pod readiness:

```
kubectl get pod -n homelab -l app=<traefik|authelia> -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}'
```

## What a sidecar scrape job needs, and three dashboard findings

Pod-role service discovery emits a target per *declared* `containerPort`, so each CrowdSec sidecar
declares 6060 (`ansible/roles/k8s/traefik/templates/deployment.yaml.j2`,
`ansible/roles/k8s/authelia/templates/deployment.yaml.j2`). A NetworkPolicy also admits
`prometheus` to that port. Either one missing gives a job that discovers nothing or reads
`up == 0` forever, both indistinguishable from the job nobody added.

Until #4088 the grant was a hand-written rule in each pod's own policy. It now renders from
`crowdsec_k8s_sidecar_agents`, one policy per app, in
`ansible/roles/k8s/netpol-baseline/templates/networkpolicy-crowdsec-sidecars.yaml.j2`.

**The netpol grant is a `from` item of its own, not a port appended to an existing rule.** A
NetworkPolicy rule ANDs its `from` with its `ports`, so adding 6060 to authelia's traefik-to-9091
rule would have admitted *traefik* to the agent's metrics and `prometheus` to nothing (#1706). It
reads like a grant in a diff. traefik's own policy already had a `prometheus` `from` item for :8080,
which is why #1694 there was a one-line port addition and #1706 here was not. The derived policy
carries the grant as its only rule, so it cannot take on another rule's caller.

Three findings the dashboards carry from this:

- A per-node selector on an engine or sidecar metric matches nothing, which is how `Alerts per
  Scenario` and `Bucket pour time` sat dead on the per-machine board (#1690).
- The overview board's agent tile is a ratio, `sum(up{job=~"crowdsec.*"}) / count(...)`, so 1
  means every agent reports whatever the fleet size. It plotted a raw count against a green step
  at 10, sized by the upstream for ten machines, so it read red on a healthy fleet of three and
  could never go green (#1709). The ratio has one blind spot, the same one `kuma-drift` names: a
  job that disappears from service discovery leaves the survivors at N/N, so the tile answers "is
  an agent down," not "is an agent missing."
- `Bucket pour time` on the per-machine board reads `{job="crowdsec"}` — the engine's AppSec pours
  alone. The traefik sidecar's own pour series are excluded on purpose, because the panel legends
  by `le` only and two datasources would collide into one set of bars.
