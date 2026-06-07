# Future Plans

Lightweight idea backlog. Detailed, ready-to-execute work lives in
[`docs/superpowers/specs/`](../docs/superpowers/specs/) and
[`docs/superpowers/plans/`](../docs/superpowers/plans/); dependency upgrades are tracked by
the Renovate dependency dashboard.

## Backlog

- _(none currently)_
- Revisit Initial Setup(sops, ansible+uv setup order)

## Superseded

- ~~Setup Authelia for Raspberry Pi~~ — `daniel-pi` is now intentionally **LAN-only**
  (`host_vars/daniel-pi.yml`: `expose_mode: lan`, all services `use_authelia: false`),
  reached over WireGuard rather than an internet-exposed, Authelia-gated ingress. Re-open
  only if a Pi service ever needs internet exposure.
