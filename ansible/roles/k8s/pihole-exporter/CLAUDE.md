# pihole-exporter — Pi-hole's DNS statistics as Prometheus series

`ekofr/pihole-exporter` polling Pi-hole's v6 admin API and serving `pihole_*` on `:9617`. One
Deployment, one Service, no volume, no route, no secret of its own.

**Deploy tag:** `--tags "pihole-exporter"`.

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
authenticating, but nothing rolls the pod for you. Redeploy the role:
`./scripts/deploy.sh --tags "pihole-exporter"`.

## Two things that bite

**The probes are `tcpSocket`, not `httpGet` on `/metrics`.** A kubelet probe against a metrics
route is the shape `ansible/tests/k8s/test_probes_do_not_flood_metrics.py` refuses below a 300s
period, after node-exporter's probes produced 97% of this namespace's Loki ingest. Whether
Pi-hole is reachable is Prometheus's `up{job="pihole"}` to report, not a reason to restart this
pod.

**`k8s_autodeploy` is `true` here and `false` on the pihole role**, deliberately. A failed
pihole deploy breaks name resolution fleet-wide; this pod holds no DNS path at all, so a bad
image bump costs dashboards.

## NetworkPolicy

The pod carries `netpol-baseline: enforced`, which is what admits Prometheus to it. Its
outbound polls are admitted by the `app: pihole-exporter` podSelector added to
`netpol-baseline/templates/networkpolicy-pihole.yaml.j2`, on port 80 — a podSelector and not an
ipBlock, because pod → ClusterIP → pod traffic keeps the caller's pod IP.
