---
name: new-k8s-service
description: Add a new k3s workload to this homelab — the role skeleton, which sibling to copy, where the entry goes in containers_list and why its position matters, secrets, and the first deploy. Use when adding any service to the cluster, or when a new role deploys nothing and you need to know what was missed.
allowed-tools: Read, Write, Edit, Grep, Glob, Bash
---

# Adding a k3s service

A new service belongs in `ansible/roles/k8s/<name>/` unless it must run on `daniel-pi`
(LAN-only utilities, WireGuard). `daniel-server` and `daniel-box` have no Docker at all, so
a Compose role there deploys nothing — for the Pi, use the `new-container` skill instead.

## 1. The role

Create `ansible/roles/k8s/<name>/tasks/main.yml` plus the manifest templates it needs:
`deployment.yaml.j2`, `service.yaml.j2`, `ingressroute.yaml.j2`, `pvc.yaml.j2`.

Copy the shape from a close sibling rather than writing one from scratch:

| The service is… | Copy |
|---|---|
| a plain web app | `ansible/roles/k8s/freshrss` |
| on the media volume | `ansible/roles/k8s/sonarr` |

**Name every `volumes[].name` for the workload or component that owns it** — `sonarr-config`,
never `config` — so a mount reads unambiguously in a diff or a `kubectl describe`. ENFORCED by
`ansible/tests/k8s/test_volume_names_descriptive.py`, which also catches the half-finished rename
(a `volumeMounts` entry with no matching volume) that no schema check can see.

`templates/` is for **manifests only**: `validate/k8s_manifests.py` renders every `*.j2` there
and parses it as YAML. App config a manifest embeds via `lookup()` goes in `templates/config/`,
static assets in `files/`. `Dockerfile*` is exempt and may sit in `templates/` directly.

## 2. The inventory entry — position matters

Add the service to `containers_list` in `ansible/inventory/host_vars/daniel-box.yml` with
`platform: k8s`.

That play has **no toposort and runs in list order**, so place the entry:

- *after* `traefik`, which installs the CRDs its IngressRoute needs, and
- *after* `authelia` if the route uses the `authelia` middleware.

An entry in the wrong position fails on a CRD that does not exist yet, which reads as a
manifest error rather than an ordering one.

## 3. Secrets

Add to `ansible/vars/secrets.yml` (`sops ansible/vars/secrets.yml`) and reference as
`{{ variable_name }}` in a `secret.yaml.j2`. The `add-secret` skill covers the rotation
registry that has to be updated alongside.

`kubectl apply` leaves **stale Secret keys** behind: removing a key from the manifest does not
remove it live. Patch it out and verify.

## 4. Deploy

```bash
./scripts/deploy.sh --tags "<name>"
```

The `deploy` skill covers the wrapper's exit codes and the `--check` / `--dry-run` / `prek`
split. Two limits specific to a *brand-new* service:

- `--dry-run` only half-checks it. `volume-claim` is skipped (it is a dependency of 25 roles
  and mutates), and nothing at admission verifies that a referenced PVC exists — so the
  Deployment validates while the volume is never proven provisionable.
- A green dry run says nothing about scheduling, PVC binding, probe or rollout behaviour.
  Those need a real deploy, gated with
  `uv run python scripts/diagnostics/probe.py health <name>`.

## 5. Verify the service, not just the pod

`probe.py health` proves the Deployment rolled out and nothing restarted in 180s. It cannot
see a broken UI behind a healthy pod, and an Authelia 302 fires in the middleware before the
backend is reached. Exercise the thing you added — the `homelab-ui` MCP server drives a real
browser against the LAN route, and `docs/claude-tooling.md` covers it.
