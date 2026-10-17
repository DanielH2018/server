# pihole-exporter — Pi-hole's DNS statistics as Prometheus series

`ekofr/pihole-exporter` polling Pi-hole's v6 admin API and serving `pihole_*` on `:9617`. One
Deployment, one Service, no volume, no route, no secret of its own.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=pihole-exporter` with exit 5
- **Image:** `ekofr/pihole-exporter` (`pihole_exporter_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (D5.5, the first Wave A PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. Its
  image-bump PRs therefore draw no `land-wait` and no `probe.py health` run. To give it back to
  Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`,
  and no Flux path runs it. It stays for the handback. Flux applies the same render, and the
  Kustomization's `wait` replaces the rollout wait.
  `k8s_autodeploy` no longer gates an apply, because the deployer skips the tag. It still
  decides whether Renovate automerges the image bump.
- **Its Kustomization has no `dependsOn: pihole`.** pihole is still Ansible-owned, so the
  renderer drops the edge and records it as `homelab/ansible-owned-deps: pihole`. The edge
  becomes a real `dependsOn` in the first render after pihole moves to Flux. The `pihole-env`
  Secret exists already, so the drop changes nothing at apply.

## Why an exporter and not a native scrape

Pi-hole v6 serves no Prometheus endpoint. Measured against the live pod on 2026-09-10:

```
$ curl -o /dev/null -w '%{http_code}\n' http://<pihole ClusterIP>/metrics
404
$ curl -o /dev/null -w '%{http_code}\n' http://<pihole ClusterIP>/api/info/version
401
```

The v6 JSON API exists and is authenticated, but nothing there speaks the exposition format.
`eko/pihole-exporter` v1.0.0 is the release that added v6 support. **Re-check the 404 before
assuming this role is still needed** — an upstream that grows `/metrics` makes the whole role
redundant, and a scrape job pointed straight at the Pi-holes would be simpler and cheaper.

## Both instances, and why that is the whole point

DNS is load-balanced across two independent FTLs (`pihole` and `pihole-2`), each keeping its
own `pihole-FTL.db`. A scrape that reached one of them would report roughly **half** the
house's query volume — silently, with a graph that looks entirely plausible.

So `PIHOLE_HOSTNAME` carries both Services, and the exporter labels every series with
`hostname`. **A panel must sum across that label**; one that filters to a single hostname is
back to reporting half.

Instance 2's Service, `pihole-2-web`, was added to the pihole role for this. It has no
IngressRoute and must not get one: the admin UI is deliberately pinned to instance 1, because a
session round-robined across two FTLs 401s at random (the comment on the `pihole` Service says
so at the line).

## It holds no credential of its own

`PIHOLE_PASSWORD` comes from a `secretKeyRef` into the `pihole-env` Secret the **pihole** role
renders — the same value FTL itself uses. One copy of that password in the cluster, one place
to rotate it. That cross-role read is why the `containers_list` entry declares
`depends_on: [pihole]`: the Secret has to exist before this pod starts.

**Rotating `pihole_password` needs this pod rolled by hand.** A Secret change alone does not
roll a Deployment, and the pihole role's restart tasks sequence its own two FTL pods and
nothing else — so after a rotation this exporter keeps presenting the old password. That is
visible rather than silent, since `up{job="pihole"}` drops to 0 once the old value stops
authenticating, but nothing rolls the pod for you.

Flux does not roll it either. The restart checksum the renderer stamps hashes only the Secrets a
tag renders itself, and `pihole-env` is the pihole role's. The flip therefore needed an entry
in `OUTSIDE_ROLE_EXEMPT` in `scripts/renderer/tests/test_renderer_restart.py`, which records
this trade. `deploy.sh --tags pihole-exporter` exits 5 since D5.5, so no agent path rolls the
pod. The operator rolls it with the admin kubeconfig (**operator-only**):
`sudo k3s kubectl -n homelab rollout restart deployment/pihole-exporter`. That leaves a
`kubectl-rollout` field manager on `restartedAt`, which Flux's next apply of the tag strips,
so the pod rolls once more then.

## Two things that bite

**The probes are `tcpSocket`, not `httpGet` on `/metrics`.** A kubelet probe against a metrics
route is the shape `ansible/tests/k8s/_scrape_property_rows.py` refuses below a 300s
period, after node-exporter's probes produced 97% of this namespace's Loki ingest. Whether
Pi-hole is reachable is Prometheus's `up{job="pihole"}` to report, not a reason to restart this
pod.

**`k8s_autodeploy` is `true` here and `false` on the pihole role**, deliberately. A failed
pihole deploy breaks name resolution fleet-wide; this pod holds no DNS path at all, so a bad
image bump costs dashboards. Under Flux the flag decides only whether Renovate automerges the
bump; `promote`'s verification gates the apply.

## NetworkPolicy

The pod carries `netpol-baseline: enforced`, which is what admits Prometheus to it. Its
outbound polls are admitted by the `app: pihole-exporter` podSelector added to
`netpol-baseline/templates/networkpolicy-pihole.yaml.j2`, on port 80 — a podSelector and not an
ipBlock, because pod → ClusterIP → pod traffic keeps the caller's pod IP.
