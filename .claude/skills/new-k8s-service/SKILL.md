---
name: new-k8s-service
description: Add a new k3s workload to this homelab — the role skeleton, the containers_list entry and how its deploy ordering is derived automatically, secrets, and the first deploy. Use when adding any service to the cluster, or when a new role deploys nothing and you need to know what was missed.
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

**Every role under `roles/k8s/` declares `k8s_autodeploy` and `k8s_autodeploy_reason` in
`defaults/main.yml`.** `ansible/filter_plugins/k8s_autodeploy.py` derives the GitOps
auto-deploy denylist from those declarations. It raises at template time on a role that
declares nothing, so a missing stance fails `initial_setup.yml --tags gitops_deploy` rather
than defaulting to either answer. Declare `true` for an ordinary service whose image pin
Renovate bumps. Declare `false` for a role that deploys no workload of its own, or one an
unattended image bump could break, and say which in the reason — the reason is what makes the
stance reviewable. Copy the shape from a sibling:

```yaml
k8s_autodeploy: false  # noqa var-naming[no-role-prefix]
k8s_autodeploy_reason: "deploys no workload of its own — …"  # noqa var-naming[no-role-prefix]
```

A `false` declaration also needs the extra command in step 4.

## 2. The inventory entry — ordering is automatic, position is not

Add the service to `containers_list` in `ansible/inventory/host_vars/daniel-box.yml` with
`platform: k8s`. Where in the list doesn't matter: the k8s play toposorts on
`build_k8s_dep_map` / `toposort_containers` (`ansible/filter_plugins/toposort.py`), and the
two edges that used to require hand positioning are derived automatically —

- an entry whose templates render a Traefik CRD (an `ingressroute.yaml.j2` using the shared
  `ingressroute.yml.j2` macro counts) gets an edge onto `traefik`, and
- an entry with `use_authelia: true` gets one onto `authelia`.

If the new entry needs an ordering constraint no template carries — something like
crowdsec's LAPI-credential edge onto traefik — declare it with `depends_on: [<name>]` on the
entry rather than moving it in the list.

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

**A role declaring `k8s_autodeploy: false` heals itself within two ticks, but you can render it
now.** The deployer re-renders its own config.env when the baked denylist disagrees with the
declarations at its checkout's HEAD (`deploy_phases.reconcile_denylist`, #1294), so the command
below is what you run to skip the wait — roughly ten minutes after the tick fast-forwards your
role in — rather than the only thing that ever fixes it. Run it after the role lands on master:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags gitops_deploy
```

The denylist is baked into `/etc/gitops-deploy/config.env`, and only that playbook renders it.
`deploy.sh` runs deploy.yml, which runs no setup role, so the new role's declaration reaches the
host only through that playbook — by your hand here, or by the deployer running it for itself on
the tick after the fast-forward. Until it is re-rendered, the deployer reads a denylist at origin
that disagrees with its own config and disarms image-pin auto-deploy for **every** service, not
just the new one, logging to `journalctl -u gitops-deploy.service`:

```
k8s auto-deploy disarmed — stale denylist (denied at origin but not in config: ['<name>'])
```

`changed=0` on the *Write deployer config* task means the host was already current. Render
after the push, not before: rendering from an unpushed tree produces the opposite mismatch,
which the deployer reports as *in config but not at origin*. A `k8s_autodeploy: true`
declaration adds no denylist entry and needs none of this.

## 5. Verify the service, not just the pod

`probe.py health` proves the Deployment rolled out and nothing restarted in 180s. It cannot
see a broken UI behind a healthy pod, and an Authelia 302 fires in the middleware before the
backend is reached. Exercise the thing you added — the `homelab-ui` MCP server drives a real
browser against the LAN route, and `docs/claude-tooling.md` covers it.
