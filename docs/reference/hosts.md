---
generated_from: scripts/docs/reference/hosts.py
generated_at: 2026-11-20 18:17 UTC
generated_sha: fd213a5e7
---

!!! warning "Generated file — do not edit"
    This page is rendered from this repository by `scripts/docs/reference/hosts.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Hosts

3 host(s) in `inventory/host_vars/`.


## daniel-box

k3s server / control-plane node. The host-package builder, the docs build, landings and most workloads run here.

| Fact | Value |
|---|---|
| LAN address | `10.0.0.215` |
| Host package | `homelab-daniel-box` (45 components) |
| Services declared | 71 |
| Has Docker | no |

## daniel-pi

Raspberry Pi, and the only remaining Docker host. LAN-only utilities.

| Fact | Value |
|---|---|
| LAN address | `10.0.0.139` |
| Host package | `homelab-daniel-pi` (24 components) |
| Services declared | 4 |
| Has Docker | yes |

## daniel-server

k3s agent node. Intel iGPU for transcoding, LVM storage, and the UPS hardware behind the NUT shutdown chain.

| Fact | Value |
|---|---|
| LAN address | `10.0.0.161` |
| Host package | `homelab-daniel-server` (29 components) |
| Services declared | 0 |
| Has Docker | no |
