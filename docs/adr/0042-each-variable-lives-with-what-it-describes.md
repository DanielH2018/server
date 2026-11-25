---
id: "0042"
title: Each variable lives with what it describes, and inventory/ is dissolved
status: Proposed
date: 2026-11-25
governs: []
---

# ADR-0042: Each variable lives with what it describes, and inventory/ is dissolved

## Status

Proposed on 2026-11-25. The operator asked for the end state, and then for this record and its
plan. The operator has not approved the open rulings listed at the end.

[ADR-0030](0030-the-ansible-tree-is-renamed-for-what-it-holds.md) named `inventory/`. Its other
rows stay in force, so its status does not change. The order of work is in
[the migration plan](../variable-layout-migration.md).

## Context

`inventory/` keeps Ansible's layout: `group_vars/all.yml` for every host and one
`host_vars/<host>.yml` each. Ansible is gone
([ADR-0029](0029-each-host-is-configured-by-its-own-package-not-ansible.md)), and the layout has
three costs.

- **A host is described in two trees.** `hosts/daniel-box/package.yml` says what the host runs,
  and `inventory/host_vars/daniel-box.yml` says with which values.
- **The host list has two sources.** `lib.inventory_lists` lists `host_vars/*.yml`, and other
  tools glob `hosts/*/package.yml`.
- **Every k8s role renders with one host's values merged in.** `lib.estate:PLANE_HOSTS` gives the
  k8s plane daniel-box's whole layer. A role reads a daniel-box value by bare name, and nothing
  at the read says so.

Measured on 2026-11-25, by rendering all 71 tags in placeholder mode without daniel-box's layer:

| Key | Reader | Without the host layer |
|---|---|---|
| `render_gid` | jellyfin, tdarr | the render fails |
| `claude_code_triage_enabled` | artifacts | `default(false)` drops a volume, silently |
| `k3s_agent_node_ips` | registry-verify | `default([])` drops a Job, silently |

No other daniel-box key changes a rendered byte. With those three kept, and 17 host-only keys
also removed from `all.yml`, the 612 rendered files are identical to the baseline. The
host-package renders were not part of this test.

The override scan on the same day found:

- no `all.yml` key repeats a role or component default;
- each host key either overrides a host-plane `all.yml` key, overrides a component default, or
  declares a value no other layer holds, such as `server_ip`;
- no host key overrides a k8s role default.

## Decision

`inventory/` is deleted. Each value lives in the tree of the thing it describes.

```
estate/vars.yml           facts about the whole estate: domain, tz, lan_subnet
k8s/vars.yml              facts about the cluster: k3s_pod_cidr, VIPs, k8s_namespace
hosts/vars.yml            defaults every host shares: has_*, docker_engine_packages
hosts/<host>/package.yml  what the host runs
hosts/<host>/vars.yml     its values, compose_services included
```

The layers, weakest first:

```
host render:     component defaults < estate < k8s < hosts < hosts/<host>
package render:  component defaults < estate < k8s < hosts
k8s render:      role defaults      < estate < k8s
```

- **Subject decides the file, not reader.** A host component that reads `k3s_pod_cidr` does not
  move it. `hosts/vars.yml` is the exception that visibility forces: a k8s render cannot read
  it, so a host-plane value that a k8s role reads, such as `weekly_reboot_hour`, is an estate
  value.
- **A k8s role names the host it reads from.** It writes `hosts[k8s_primary_node].render_gid`.
  `PLANE_HOSTS` is deleted.
- **A shared key has one file.** A check refuses a key declared in two of `estate/vars.yml`,
  `k8s/vars.yml` and `hosts/vars.yml`, and refuses one that repeats a role or component
  default.
- **A host file may add or override, within bounds.** It may declare a new key, or override a
  `hosts/vars.yml` key or a component default. It never repeats an estate or k8s key.
- **A host is a directory holding `package.yml`.** The host list derives from that alone.
- **`lib.estate` is the only checkout module that knows these paths.**

The k8s file sits beside the service list, which
[PR 10093](https://github.com/DanielH2018/server/pull/10093) prepares to move from `cluster/` to
`k8s/`. The rejected placement, `cluster/vars.yml`, would recreate the directory that move
empties.

## What was rejected

- **Moving all of `inventory/` into `hosts/`.** 43 of the 86 `all.yml` keys describe the
  cluster. They would sit in a directory named for hosts.
- **Moving only `host_vars`.** It fixes the first two costs and leaves one half-empty directory
  for the shared file.
- **Placing each key by who reads it.** 41 of the 86 keys are named in both `hosts/` and `k8s/`,
  and adding a reader would move a key.
- **A root `estate.yml`.** homelab-mcp mounts checkout directories from its node. A single-file
  mount holds the old inode after git replaces the file, so the pod would serve stale content.
- **A separate `hosts/daniel-pi/services.yml`.** `compose_services` is a host value that
  `hosts['daniel-pi']` already carries to the k8s templates. A second file adds a layer for one
  key.

## Consequences

- The migration reaches about 130 files outside `docs/archive`, so it costs a pause in the
  fan-out and a rebase of open branches for its two move PRs.
- Three gates match `inventory/` by path and would stop matching, with no error: the Renovate
  agent's landing refusal, the T2 approval floor's classification, and the scan in
  `scripts/validate/cluster_pins.py` for a pin set outside its role. Each must match the new
  paths before a file moves.
- `renovate.json` names `inventory/group_vars/all.yml` in seven places. Each entry that misses
  the move stops bumping its pin and reports nothing.
- A key that one plane reads can be promoted to the estate only by moving it, so `git log` on
  the key crosses a file.
- Reversing this after the last step means a second migration of the same size.

## Open rulings

The operator settles these before step 3 of the plan:

1. **The name `estate/`.** It matches `lib/estate.py`.
2. **`k3s_agent_node_ips` moves to `k8s/vars.yml`.** Its value is Jinja that reads
   `hosts['daniel-server']`. `render_context` passes no Jinja value through `hosts`, so a role
   cannot read it from daniel-box's file. The key describes the cluster.
3. **Whether a Renovate PR that changes `k8s/vars.yml` stays refused.** The file holds
   `crowdsec_k8s_image`. The plan keeps the refusal that `inventory/` has.
4. **Which directories homelab-mcp serves after the move.** It serves `k8s/`, `inventory/` and
   `tests/`. Serving host values means serving `hosts/`, which is a wider read than
   `inventory/` was. The plan serves `estate/` and drops host values unless the operator widens
   it.

## Governs

No line carries a marker, because nothing is built. The PR that adds the layer check adds the
marker and sets this record to Accepted.
