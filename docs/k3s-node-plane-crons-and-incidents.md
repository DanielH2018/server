# k3s node plane: the backup crons' derivations and the measured incidents

Working-out moved off `ansible/roles/setup/k3s/CLAUDE.md` (#2997), which a session reads on every
touch of the node plane. The role doc keeps the autonomous-role contract, the actuator scope and
the `ENFORCED:` citations; this page keeps the restore drill's two waivers and its retry bound,
the cron-evidence checks that make a `logger` line evidence at all, and the four incidents behind
the role doc's `## Notable`.

## The restore drill's waivers and its retry bound

The drill restores the newest backup of one eligible volume into a new volume, mounts it in a
throwaway pod, checks the data is real, and tears everything down. Three refinements bound it.

**An operator pin drills one volume without disturbing the rotation** (#2183). Pass the PVC name:
`sudo /usr/local/bin/longhorn-restore-drill.sh <pvc>`, or `RESTORE_DRILL_PIN=<pvc>` for a caller
that cannot pass an argument. A hand run stamps `success/<pvc>` but not `last-success`, so it
proves the volume without hiding a dead nightly cron from check 7, and it leaves the published
candidate list whole so check 8's coverage window keeps its size. Backdating a stamp under
`attempts/` to steer the rotation is no longer the way in.

**A volume whose content is legitimately empty is declared, and the declaration is bounded by
size** (#2393). The name goes in
`ansible/roles/setup/k3s/defaults/main.yml:k3s_longhorn_restore_drill_empty_ok_pvcs`, and the
drill waives its two content assertions for that name — everything else it proves for that volume
it still proves. n8n-files restored to `files=0` on 2026-08-30, could not be re-drilled for
another rotation, and check 8 paged `not restore-proven in 31d`. The name is not enough on its
own: the waiver holds only while the source volume's `status.actualSize` stays under
`ansible/roles/setup/k3s/defaults/main.yml:k3s_longhorn_restore_drill_empty_ok_max_actual_bytes`.
By name alone it applied whatever the volume held, and nothing else in the drill compares restored
content with the source, so once n8n-files starts holding data an empty restore would still have
stamped success and check 8 would still have read it as restore-proven. Over the ceiling the
waiver is withheld rather than the run failed, so a declared volume that has filled up and
restores correctly passes on the ordinary assertions.

**A failed attempt is re-drilled the next night, once per failure** (#2270). Selection is
least recently attempted and the attempt stamp is refreshed whatever the outcome, so before this a
failed volume waited a full 26-night cycle against check 8's 31-day window and paged first. The
bound is a marker under `retries/`, stamped with the attempt: a volume that fails its retry waits
for its own rotation slot, so a permanently broken volume costs two nights a cycle rather than
every night.

Eligibility is the `recurring-job-group.longhorn.io/*` label, with the `no-backup` group excluded
by name. The volume is chosen least recently attempted, not least recently succeeded, so one
permanently broken volume cannot starve the rest.

## A `logger` line is evidence only because two checks read it

The trim, the drill and the two B2 accounting crons log every run through `logger`, readable in
Loki. That is evidence only because check 9 of `longhorn-backup-health.sh` reads it (#2418). It
pushes DOWN on the trim's `ABORT:` line, on a non-zero `N failed` in its newest summary line, or
on an `UNPRICED` line from `b2-deletions` anywhere in
`ansible/roles/setup/k3s/defaults/main.yml:k3s_longhorn_cron_evidence_window_hours`. Before that
the trim's own comment claimed "a cron mail or a Kuma push notices" a failure: no such push
existed, and the cron mail lands in `/var/mail/ubuntu`, which held 5,678 unread messages on
2026-09-24.

**Check 10 is the second reader, for the crons' liveness** (#2443). Check 9 reads what they said,
and an empty window reads as green to it. Check 10 pushes DOWN when the window holds no line a
completed run of that cron writes — its `/etc/cron.d` entry removed, its script un-rendered, the
crontab lost, or the run itself dying part-way — which is the shape check 7 closes for the restore
drill. Each arm judges on a recognised line, the trim on its summary or `ABORT:` and
`b2-deletions` on the summary line `probe.py b2-deletions` ends every completed run with (#2545);
before that the b2 arm took any line at all, so a run that logged a traceback and exited non-zero
read as alive. It stays quiet in the three cases that produce silence legitimately: a window
shorter than the 24h cron period, a cron this host does not install (`b2-deletion-accounting` is
gated on `has_repo_checkout`), and a cron installed less than one window ago.

**Check 10 tells "fired and failed" from "stopped firing" with a fire stamp** (#3677).
`initial_setup` hardens `/etc/cron.d` to 0700 root, so the `sys_user` this check runs as can
never stat a cron's entry there. The trim script and the `b2-deletions` cron line each touch a stamp under
`k3s_cron_fired_stamp_dir` before its job runs, and the shim exports the stamp paths. A stamp
inside the window means the cron fired and its run did not complete, and the message points at
the cron's journal tag. A stale or missing stamp means the cron has not fired. The message
then says that whether the entry is still installed needs root to check. The 2026-10-08 outage
was the first kind: `b2-deletions` fired every day and logged only its refusal.

Every matched line is matched as written, so rewording one means changing `_TRIM_SUMMARY_RE` /
`_TRIM_ABORT_RE` / `_DELETIONS_SUMMARY_RE` / `_DELETIONS_DECLINED_RE` in
`ansible/roles/setup/k3s/files/longhorn_lib/longhorn_cron_evidence_logic.py` in the same edit. The b2 shapes
come from `deletions_summary_line` / `deletions_declined_line` in
`scripts/diagnostics/probe_lib/b2_ledger.py`, which
`test_cron_liveness_accepts_the_summary_line_the_probe_writes` holds to the regexes.

The drill stamps `ATTEMPT_DIR` on every run and `SUCCESS_DIR` only after the assertions pass;
check 7 reads those and pushes DOWN when the drill stops running — a drill that silently stops
looks identical to one never scheduled, which is how the hand-run drill of 2026-08-16 went
unrepeated until the cron existed.

## The read-only crons, and the two that became timers

`Longhorn backup health`, `daniel-box disk health`, `Manifest prune drift check`, `Release
staleness drift check`, `Live object drift check`, `B2 deletion accounting`, `B2 backup budget
listing` and the `--list-only` etcd drill read the cluster or the bucket and write nothing to
either. The heartbeats push a Kuma tile through `kuma-push-lib.sh`, except `daniel-box disk
health`: since M3's PR 5 its heartbeat is a textfile metric (`disk-health.prom`) that the
`host-checks` rules page through Alertmanager, and it pushes no Kuma tile. The B2 accounting pair
appends to the local ledger, which is bookkeeping, not state.

`Manifest prune drift check` and `Live object drift check` are kuma-check timers rather than crons
since 2026-09-19, importing `setup/common`'s `kuma_check_timer.yml`. Each script exits 1 after it
pushes `down`, and the service's `Restart=on-failure` reruns it every 30 min until it exits 0, so
a red tile clears when the fault does rather than at the next slot. The timers are
`Persistent=true`, and both checks carry the boot grace so a catch-up run at boot exits 1 without
a verdict and the restart carries the real one. A third, `remember log rotation health`, was
retired on 2026-09-28 with the remember plugin (#2852). `systemctl status kuma-check-<name>` shows
`auto-restart` while red. Manifest prune's healthchecks.io `/fail` ping repeats on every rerun;
healthchecks notifies on a status change, so a check already down is not paged again.

## k3s's own output is in `/var/log/k3s.log`, not the journal

journald stores nothing below notice on these hosts and every line k3s writes is priority info —
logrus and klog emit plain text to stderr with no `<N>` prefix, so INFO, WARN and FATA all take
the unit's `SyslogLevel=info` default. On 2026-09-09 k3s crash-looped ~3000 times over 5h08m and
the journal kept only systemd's `status=1/FAILURE` lines (#1918). `tasks/unit-logging.yml` writes
a `StandardOutput=append:` drop-in for `k3s.service` and `k3s-agent.service`, plus a `copytruncate`
logrotate stanza — truncate, because systemd holds the fd and a rename would leave the unit
writing to the rotated inode. A drop-in rather than the unit because `k3s-install.sh` rewrites the
unit file wholesale. Raising the unit's `SyslogLevel` to notice was the alternative and was
rejected: notice passes the rsyslog info filter, so every k3s line would land in `/var/log/syslog`
and ship to Loki. `ansible/tests/setup/test_k3s_unit_logging.py` holds both node types to it.

## The apiserver's OIDC authenticator can wedge for the life of the boot

Nothing pages when it does (#2749). `k3s_oidc_issuer_urls` in
`ansible/roles/setup/k3s/defaults/main.yml` points the apiserver at two names Authelia serves
behind Traefik, so every boot races workloads this same apiserver schedules. `oidc.go` retries the
discovery fetch every 10s, which normally makes the race free. It is not free when Traefik answers
the first fetch with a 421: the connection keeps that answer, and the apiserver never redials. The
mechanism is in the Traefik record's *Why a 421 outlives the misconfiguration that caused it*
(`docs/traefik-plugins-and-startup.md`).

On 2026-09-27 the wedge left 3924 `oidc authenticator: initializing plugin` errors in
`/var/log/k3s.log` from 07:40:23Z with no success, one per issuer, so Headlamp login was dead for
5.5 hours.

**The tell is which auth still works:** client certificates and every ServiceAccount are
untouched, so kubectl, the controllers and every probe read green throughout. To recover without
restarting the control plane, close **both** sockets — `sudo ss -tnp | grep k3s-server` on
daniel-box names them, one per issuer, and healing only the VIP one leaves the public issuer's
errors running — then let the next 10s tick redial. The full sequence, the recovery caveat and the
measured error chain are in that defaults file's comment above the key.

## The release-staleness check is the durable half of a one-shot Discord page

When the deployer defers a k8s change it cannot auto-apply (`deploy_alerts.alert_deferred`'s
`cs.k8s` branch), it fast-forwards the tree and pages once per SHA. The ff-merge clears the
deployer's own `behind_since`, so nothing else says the cluster is still running old manifests.
`release-staleness-check.sh` runs `probe.py releases --stale-only` every
`k3s_release_staleness_cron_minute` and pushes the tile down while any service's applied commit
sits behind `origin/master` under its role paths, or under an inventory key or shared macro its
render reads (#1993). A merge younger than `k3s_release_staleness_grace_minutes` is named in the
`up` message and not counted, so a landing still deploying its own merge does not page. The full
account, including why it runs as `sys_user` and not root, is the `#947` bullet in
`ansible/roles/setup/gitops_deploy/CLAUDE.md`.

**The DOWN message carries the stale services' names and a count, not their reasons** (#2013). A
refused narrowing marks the whole fleet stale, and the per-service reasons then ran to ~7,500
chars. Kuma puts the message into a Discord embed field capped at 1024 chars and never truncates,
so Discord rejected the DOWN with HTTP 400 on 2026-09-17 and 2026-09-18 and the page reached
nobody. `kuma_push` (kuma-push-lib.sh) and the bridge's `net.push` both cap the message at 900
chars as the class fix; the reasons are `probe.py releases --stale-only`.

**A changed stale set re-alerts while the tile stays DOWN** (#2378). Kuma notifies on a status
transition only, so a DOWN whose set grew or shrank reached nobody. The cron keeps the set the
last DOWN pushed in `/var/lib/homelab/release-staleness/stale-set` (written by `probe.py releases
--names-out`). When the set differs, the cron pushes an `up` that opens `Not a recovery.`, then
the `down` with the added and cleared names prefixed. A DOWN that is not a verdict (the fetch
failing, or the probe exiting above 1) keeps the recorded set, or records an empty one so the
first verdict after it still notifies. An `up` clears it.

## Both nodes pin their LAN address, because k3s binds it

k3s binds `server_ip` itself, through etcd's peer listener and the apiserver's advertise
address. A node that takes its address from DHCP cannot start k3s while the gateway's DHCP
server is down. From the 2026-10-05 23:55 reboot to 2026-10-08 21:55, an internet outage took
the gateway's DHCP with it. k3s crash-looped every ~6s on `listen tcp 10.0.0.215:2380: bind:
cannot assign requested address`, and no CronJob fired for ~70h (#3882). eno1 kept carrier
throughout: syslog has `NIC Link is Up` at 2026-10-05T23:55:15 and no `Link is Down` until
2026-10-08T21:03:41.

`tasks/static-address.yml` renders `/etc/netplan/90-homelab-static-address.yaml` on the link
`k3s_node_static_link` names. It switches DHCPv4 off on that link, pins the address and a
default route via `lan_router_ip`, sets `ignore-carrier`, and leaves IPv6 router
advertisements alone. DNS never came from the lease, because this role renders
`/etc/resolv.conf`. The template says why DHCPv4 is off rather than running alongside. The
gateway must keep the address out of its DHCP pool, since with DHCPv4 off nothing renews a
lease on it. To apply it, run `k3s-bringup.yml --tags node-address` on daniel-box.
To back the pin out, empty `k3s_node_static_link` and run the same tag. That run removes the
file and re-applies netplan, so the link returns to the installer's DHCP.

daniel-server pins its address too (#3891), on `enp88s0`. The gateway reserves 10.0.0.161 for
that NIC, and wlo1's DHCP route at metric 600 stays as the fallback. `ignore-carrier` keeps
the pinned metric-100 routes installed when the cable drops. By default, IPv4 still routes
over a link that has lost carrier. The same task file therefore sets
`ignore_routes_with_linkdown = 1` for both `net.ipv4.conf.<link>` and `net.ipv6.conf.<link>` in
`/etc/sysctl.d/90-homelab-static-link.conf`, so route lookups skip the link while it has no
carrier and wlo1 wins (#4510). To check it, unplug `enp88s0` and run `ip route get 1.1.1.1`,
`ip route get 10.0.0.215` and `ip -6 route get 2606:4700:4700::1111`; all three must name `wlo1`.

The IPv6 default route arrives by router advertisement, not from the pin. `systemd-networkd` gives an RA
route the link's `[DHCP] RouteMetric`, or 1024 when there is none. netplan 1.1.2 has no
`ra-overrides` metric key and writes `[DHCP]` only while a DHCP client is on. `dhcp4: false`
therefore dropped enp88s0's RA route to 1024, behind wlo1's 600, and IPv6 egress took wlo1
(#4561). The template sets `dhcp6: true` with `dhcp6-overrides: {route-metric: 100}`. That adds
no client in practice: the gateway's RA sets the M flag, so `systemd-networkd` already ran DHCPv6 on the
link. With both links up, `ip -6 route get 2606:4700:4700::1111` must name `enp88s0`. The join play cannot
apply the pin: it reaches daniel-server over SSH through a dynamic `include_role`, which
`--tags node-address` cannot select, and a `netplan apply` there would reconfigure the link
the session uses. A separate opt-in play imports the same task file statically and runs on
daniel-server itself. To apply or back out the pin there, run this on daniel-server:

```bash
uv run ansible-playbook ansible/k3s-bringup.yml \
  -e pin_agent_address=daniel-server --tags node-address
```

While `pin_agent_address` is set, the server play matches no hosts, so its server-host guard
does not refuse the agent. The run touches nothing of the agent join.

## Both nodes fall back to WiFi when ethernet loses carrier

The same task file joins each node's WiFi link, named by `k3s_node_wifi_link`, to the home
network. It renders `/etc/netplan/80-homelab-wifi-fallback.yaml` from the SOPS keys
`wifi_ssid` and `wifi_password`. The link takes a DHCP address with its routes pinned at
metric 600, behind the wired link's 100. It is `optional`, so boot does not wait for it to
associate. daniel-box uses `wlp4s0`, a MediaTek MT7922. daniel-server uses `wlo1`, an Intel
AX201. The installer's `00-installer-config-wifi.yaml` already joined `wlo1`, and the role's
file merges on top of it.

The fallback depends on the pin's `ignore_routes_with_linkdown`. Without it, the pinned routes
win with no carrier, and traffic goes nowhere. With it, both nodes keep their addresses:
`server_ip` stays on the wired link through `ignore-carrier`, and the kernel answers ARP for it
on WiFi, because `arp_ignore` is 0. A peer that re-resolves the address reaches the node over
WiFi. flannel's VXLAN device records the wired link as `dev`, but its per-peer forwarding
entries carry no interface. The kernel therefore routes the encapsulated packets through the
main table, which picks WiFi once the wired routes are skipped. This paragraph is read from
the routing tables and the forwarding entries on 2026-10-15; no unplug test has confirmed it.

Because the node stays Ready on WiFi, a pulled cable or a dead switch port causes no outage
that anything else reports. Prometheus raises `WiredLinkDown`, a warning routed to Discord,
once the link that `k3s_node_static_link` names has had no carrier, or has been down, for 5
minutes. The rules live in the observability role's `host-checks` group, one per node.
`ansible/tests/services/test_host_plane_rule_literals.py` holds them to each node's
`k3s_node_static_link`, so a node that pins a new link fails that test until its rule is
added (#5124).

The same ARP behaviour has a cost with the cable in. Both links answer a broadcast ARP
request, so a client can learn the WiFi MAC for an address the wired link carries. MetalLB's
speaker builds an ARP responder on every link that is up, `wlo1` included, so on daniel-box
this covers the VIPs as well as `server_ip`. daniel-server shows no such flux: the Pi and
daniel-box both held the `enp88s0` MAC for 10.0.0.161 on 2026-10-15. Adding
`interfaces: [eno1]` to the `homelab-l2` L2Advertisement stops the flux for the VIPs, at the
price of the VIPs not failing over.

The fallback covers a failed cable, NIC or switch port on one node. It does not cover a dead
gateway, which is also the access point. A WiFi link carries a fraction of the wired
bandwidth: daniel-server's `wlo1` read a signal level of `-71 dBm` on 2026-10-15, so Longhorn replication and
media streaming slow down while it carries them.

To apply it, add both keys with `sops ansible/vars/secrets.yml`, which opens an editor and
keeps the password out of shell history. Then run
`uv run python scripts/secrets_mgmt/secret_rotation.py sync` and commit. Then run the
`node-address` tag on each node, the same two commands as the pin. The run refuses to start
while either key is missing. To back it out, empty `k3s_node_wifi_link` and run the same tag.

To check the fallback, unplug the node's cable and confirm three things. First, `ip route get
1.1.1.1` names the WiFi link. Second, the other node reaches `server_ip`, by `ping` and by
`kubectl get nodes` reading both `Ready`. Third, a pod on one node reaches a pod on the other.
On daniel-box, also check that a LAN client reaches the Traefik VIP. A client's cached
ethernet MAC can take minutes to expire.

After the first apply on daniel-box, check for flux with the cable in. On daniel-pi,
`ip neigh show` for 10.0.0.215, the Traefik VIP and the Pi-hole VIP must each name
`c8:ff:bf:0d:2c:c4`, the `eno1` MAC, and not `4c:23:38:b6:e0:43`, the `wlp4s0` MAC. A day
later, `ip -s link show wlp4s0` should show only a few hundred MB, as `wlo1` does on
daniel-server. If either check fails, empty `k3s_node_wifi_link` for daniel-box and run the
tag again.

## Two smaller traps

**Cron's PATH omits `/usr/local/bin`, where k3s lives.** Every script here sets its own PATH; a
new one that does not dies on `command -v k3s` and, if it pushes its heartbeat before the check,
reads permanently green.

**The drift checks and the prune check are staggered on purpose** (05:15 and 05:45) so two full
kubectl sweeps do not land on the API server at once. Keep a new sweep off those minutes.

## What opens the three control-plane gates under `--tags k3s`

The maximal tag reapplies Longhorn, the backup targets, the health crons, CoreDNS and
the node config, and makes three gated tasks reachable. Each is gated, so the tag *can* take the
control plane down rather than always doing so:

- `Install or reconfigure the k3s server` runs the installer, which restarts k3s, when
  `k3s_server_args` gained an argument or `k3s_version` moved.
- `Restart k3s so the log drop-in takes effect` restarts the unit when the drop-in changed and
  the installer did not run.
- `Rotate the encryption key and re-encrypt Secrets already stored in etcd` runs `k3s
  secrets-encrypt rotate-keys` when the cluster has never reached `reencrypt_finished`, which on
  a converged cluster it has.

To see what a candidate tag selects before running it, add `--list-tasks`. A backup-target change
is a PR to the `longhorn-backup` role, which Flux applies, and the BackupTarget break-glass in
`docs/break-glass.md` covers the cases a PR cannot reach; the read-only identity wants
`--tags kubeconfig`, applied
2026-09-22 with ok=15 changed=2.

The weekly `etcd restore drill` cron is gated on `k3s_etcd_restore_drill_armed` and runs
`--list-only` on this host, because a full etcd restore cannot pass beside a live k3s. Its
`--list-only` leg also runs restore gate 3 (`runbook_gates.py etcd-restore --gate 3`) against the
snapshot it listed, so the `ETCDSnapshotFile` read the runbook depends on is exercised weekly
rather than on the day of an outage (#2420); any non-zero exit fails the drill.
