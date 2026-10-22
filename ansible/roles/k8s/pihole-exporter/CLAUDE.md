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

**A `pihole_password` rotation rolls this pod through Flux.** A Secret change alone does not
roll a Deployment, and the restart checksum the renderer stamps hashes only the Secrets a tag
renders itself. `pihole-env` is the pihole role's, so the include in `tasks/main.yml` declares
it under `manifests_outside_secrets` with its one key, `pihole_password`. The renderer hashes
that key's `cluster-vars.source` digest into this Deployment's restart checksum annotation
(`scripts/renderer/renderer_lib/restart.py:outside_secrets`). The rotation commit moves the
digest, and Flux rolls the pod when it applies that revision (#5416).

The declaration also makes `secret_rotation.py consumers pihole_password` list this role,
because the census greps role files for the key name. A stale password still shows as
`up{job="pihole"}` at 0.
`scripts/renderer/tests/test_renderer_restart.py::test_every_outside_secret_declares_the_keys_its_owning_role_renders`
holds the declared keys equal to the placeholders the pihole role's `pihole-env` renders.

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
