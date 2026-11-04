### PR C-2a and C-2b

#### PR C-2a

[#7087](https://github.com/DanielH2018/server/pull/7087), from
[#7086](https://github.com/DanielH2018/server/issues/7086), deletes `uptime-kuma` from the bespoke
policies and from ical-proxy's `netpol_from`.

**One PR holds the order.** `netpol-baseline` renders all five grants, including ical-proxy's fence
through `networkpolicy-callers.yaml.j2`. No workload role's render moves, so *the roles first* has
nothing to order.

**Three of the four bespoke policies held a grant.** `networkpolicy-jellyfin.yaml.j2` leaves `:8096`
open to every source and never named Kuma as a peer. It held only a comment about Kuma's VIP tile,
and that comment is what C-2a deletes there.

#### PR C-2b

The branch `worktree-fanout-7095`, from
[#7095](https://github.com/DanielH2018/server/issues/7095), deletes the role directory, its
`containers_list` entry, and `k8s_host_files/tasks/uptime-kuma.yml` with its import.

**`ls` cannot answer the staging-directory read.** `/etc/rancher/k3s/manifests` is mode 0700, so
`ls` as the operator's account answers `Permission denied`. `rootq stat` lists the path by name and
runs as root. It answers `refused: /etc/rancher/k3s/manifests/autokuma-claims does not exist`,
while its control `/etc/rancher/k3s/manifests/registry-jobs` prints a stat line.

**The claims leave the no-backup list with the entry.** `longhorn-reconcile` then moves each
`longhorn`-class volume from `no-backup` to `default`, and `daily-backup` backs up a detached
volume because `allow-recurring-job-while-volume-detached` is `true`. The operator's delete
therefore belongs before the first 03:30 UTC after the promotion.
