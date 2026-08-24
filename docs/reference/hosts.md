---
generated_from: scripts/gen_reference_hosts.py
generated_at: 2026-08-24 20:13 UTC
generated_sha: 1e5789a8
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/gen_reference_hosts.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Hosts

3 host(s) in `ansible/inventory/hosts.ini`.


## daniel-box

k3s server / control-plane node. Ansible runs here, and so do the GitOps timer, the docs build and most workloads.

| Fact | Value |
|---|---|
| LAN address | `10.0.0.215` |
| Ansible connection | `local` |
| Service exposure | traefik (default) |
| Services declared | 53 |
| Runs the GitOps timer | yes |
| Has Docker | no |

## daniel-pi

Raspberry Pi, and the only remaining Docker host. LAN-only utilities.

| Fact | Value |
|---|---|
| LAN address | `10.0.0.139` |
| Ansible connection | `ssh` |
| Service exposure | lan |
| Services declared | 7 |
| Runs the GitOps timer | no |
| Has Docker | unknown (has_docker not declared) |

## daniel-server

k3s agent node. Intel iGPU for transcoding, LVM storage, and the UPS hardware behind the NUT shutdown chain.

| Fact | Value |
|---|---|
| LAN address | `10.0.0.161` |
| Ansible connection | `local` |
| Service exposure | traefik (default) |
| Services declared | 0 |
| Runs the GitOps timer | no |
| Has Docker | no |

## Running a playbook against a host

`hosts.ini` pins both cluster nodes to `ansible_connection=local`, so a play's `hosts:` defaults to the local hostname. **`--limit daniel-pi` therefore matches zero hosts** — target the Pi with `-e target=daniel-pi` instead. A one-shot play that ignores this runs on the wrong box while appearing to succeed.
