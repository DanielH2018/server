---
name: new-k8s-service
description: Add a new k3s workload to this homelab — the role skeleton, the containers_list entry and how its deploy ordering is derived automatically, secrets, and the first deploy. Use when adding any service to the cluster, or when a new role deploys nothing and you need to know what was missed.
allowed-tools: Read, Write, Edit, Grep, Glob, Bash
---

# Adding a k3s service

A new service belongs in `ansible/roles/k8s/<name>/` unless it must run on `daniel-pi`
(LAN-only utilities, WireGuard). `daniel-server` and `daniel-box` have no Docker at all, so
a Compose role there deploys nothing — for the Pi, use the `new-container` skill instead.

## 1. The role — scaffold it, do not copy a sibling

```bash
uv run python scripts/dev/new_k8s_service.py <name> \
    --image <repo:tag> --port <port> --uid <the image's own uid> \
    [--authelia one_factor|two_factor] [--no-route] [--strategy Recreate]
```

That appends the `containers_list` entry and writes `templates/deployment.yaml.j2`, the one
manifest the role ships (#4298). Beside it go a `tasks/main.yml` that is the bare include of
`k8s/manifests` with no vars, a `defaults/main.yml` holding the image pin, auto-deploy stance,
resources and uid, and a `CLAUDE.md`. It then runs the role-glance generator over the new doc. Its output renders clean through `prek run --all-files` with no hand edits.

**Copying a sibling is what this replaced** (#2855). A sibling's files carry its narration, and
that narration is dated — littlelink's Deployment cited a Compose template that had not existed
since 2026-08-14. Read the generated files before you extend them; the TODOs in the CLAUDE.md
are the parts only you can write.

**No `service.yaml.j2` is written, and none is needed.** `k8s/manifests` renders
`ansible/templates/service-default.yaml.j2` for a role that ships none (#2872), reading the
name and port off the `containers_list` entry. A Service needing a named port or a sidecar's
port says so on the entry, with `service_port_name` or `service_extra_ports`. Anything beyond
that — a selector that differs from the name, a LoadBalancer, a pinned clusterIP — means
writing the role its own `templates/service.yaml.j2`, which always wins over the default.
`ansible/templates/service.yml.j2`'s header lists every disqualifier and the role behind it.

**No `ingressroute.yaml.j2` is written either** (#3043). `k8s/manifests` renders
`ansible/templates/ingressroute-default.yaml.j2` the same way, reading the name, `hostname`,
`port` and `use_authelia` off the entry. A route needing anything the entry does not carry —
an extra middleware, a second hostname, `public=false`, a bypass prefix, a render condition —
means writing the role its own `templates/ingressroute.yaml.j2`; the macro header in
`ansible/templates/ingressroute.yml.j2` lists every parameter.

**The scaffolder writes no PVC, Secret or NetworkPolicy.** Each is a decision about the
service, so add the template by hand. The role passes no file list: `k8s/manifests` renders
every top-level `templates/*.yaml.j2` it finds, so a new template ships by existing. Name a
Secret's template with `secret` in its basename (`secret.yaml.j2`), or it renders 0644
outside `no_log`, which `ansible/tests/k8s/test_derived_manifest_files.py` refuses. A one-off
Job's template ends `-job.yaml.j2` and stays out of the directory apply.

**Two censuses a new role always joins**, which `prek` does not run and CI does — the
scaffolder names both when it finishes:

- `BORN_FENCED_ROLES` in `ansible/tests/k8s/test_netpol_baseline_labels.py`, with the sentence
  saying why Traefik is the pod's only caller. A service that dials out drops the
  `netpol-baseline: enforced` pod label and gets its own NetworkPolicy instead.
- `ROLES_WITH_A_DEFAULT_SERVICE` in `ansible/tests/k8s/test_shared_manifest_defaults.py`, and
  `ROLES_WITH_A_DEFAULT_INGRESSROUTE` beside it for a routed role.

The new role moves the auto-deploy coverage docs fragment. The `regen-doc-fragments` prek hook
rewrites it on your commit and fails once, so stage what it wrote and commit again.

**The pod-spec shell comes from two shared macros, which the scaffolder already calls.** A
Deployment template calls `spec_shell(strategy)` under `spec:` and `pod_shell(priority_class,
…)` under `spec.template.spec:`, both from `ansible/templates/workload-shell.yml.j2`; a
DaemonSet calls `pod_shell` only. Both required arguments are decisions, and the file's
docstring says what each one costs: `strategy` is `Recreate` (a downtime gap every deploy,
allowlisted with its reason in `ansible/tests/k8s/test_deploy_strategy.py`) or
`RollingUpdate`; `priority_class` is one of the tiers in
`roles/setup/k3s/templates/priorityclass.yaml.j2`. The pod-level `securityContext` goes
through the macro's `run_as_user`/`fs_group`/`non_root` arguments — a pod that needs sysctls
or supplementalGroups writes the whole block itself and passes none of them. ENFORCED by
`ansible/tests/k8s/test_workload_shell_uses_the_macros.py`, which refuses an owned field
written out by hand. The container-level `securityContext` is `hardened_security_context`
from `security-context.yml.j2`, as before. A CronJob's containers sit one level deeper, under
`jobTemplate`, and take the 14-space twins `job_hardened_security_context` and
`job_container_resources` instead.

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
stance reviewable. The scaffolder writes `true` unless you pass `--no-autodeploy`, and writes
a TODO in place of the reason:

```yaml
k8s_autodeploy: false  # noqa var-naming[no-role-prefix]
k8s_autodeploy_reason: "deploys no workload of its own — …"  # noqa var-naming[no-role-prefix]
```

A `false` declaration also needs the extra command in step 4.

**Every deployed role has a `CLAUDE.md` that opens with `## At a glance`, and the block under
that heading is generated.** The scaffolder writes the heading, the markers and the prose
skeleton, and runs the generator once. After any later change to the role's defaults,
templates or `containers_list` entry, the `regen-role-glance` prek hook re-runs
`scripts/docs/gen_role_glance.py` at commit time. It writes the deploy tag, image
repositories, route, claims and auto-deploy stance between two `generated_from` markers and
leaves everything below them alone. When it rewrites a block it fails the commit once, so
stage the doc and commit again. `scripts/docs/tests/test_gen_role_glance.py` fails CI only
for a commit that skipped the hook. Put the reasoning
— why a claim is unbacked, what a route bypasses — in the bullets below the block, not in
the sources it reads.

## 2. The inventory entry — ordering is automatic, position is not

The scaffolder appends the entry to `containers_list` in
`ansible/inventory/host_vars/daniel-box.yml` with `platform: k8s` and `owner: flux`, at the end
of the list but ahead of netpol-baseline. **The entry is born owned by Flux** (D5.5 of
`docs/target-architecture/deploy/d5-bulk-move.md`): every Ansible path skips it from its first
commit, so it never needs a flip PR. Delete the `owner` line only for a service that must stay
on Ansible until D5 moves it, and say why in the comment above the entry. netpol-baseline is ordered after every other entry, because it
is in `K8S_APPLIES_LAST`, and `test_derived_order_matches_todays_hand_ordered_list` holds the
list to the sorted order. Otherwise, where in the list doesn't matter: the k8s play toposorts
on `build_k8s_dep_map` / `toposort_containers` (`ansible/filter_plugins/toposort.py`), and
three edges are derived automatically —

- a routed entry — one carrying a `hostname` — gets an edge onto `traefik`, as does a role
  whose own templates render a Traefik CRD without declaring a host,
- an entry with `use_authelia: true` gets one onto `authelia`, and
- each `K8S_APPLIES_LAST` role gets one onto the new entry.

If the new entry needs an ordering constraint no template carries — crowdsec before traefik,
sonarr and radarr before configarr, dri-device-plugin before jellyfin and tdarr — declare it
with `depends_on: [<name>]` on the entry rather than moving it in the list. No template may
read `depends_on`: a deploy narrowing leaves it out of an entry edit
(`DEPLOY_MECHANICS_FIELDS` in `scripts/deploy_tools/narrow_lib/containers.py`).

`use_authelia: true` needs `auth_tier: one_factor | two_factor` beside it — the Authelia
policy for the service's LAN name, which the authelia role renders into its access_control
rules. An entry that attaches the middleware without a tier fails `validate/k8s_manifests.py`
and the deploy. Pick `two_factor` for anything that acts on a stolen password alone (a shell,
a deploy trigger, a volume delete) and say why in the comment above the entry.

## 3. Secrets

Add to `ansible/vars/secrets.yml` (`sops ansible/vars/secrets.yml`) and reference as
`{{ variable_name }}` in a `secret.yaml.j2`. The `add-secret` skill covers the rotation
registry that has to be updated alongside.

`kubectl apply` leaves **stale Secret keys** behind: removing a key from the manifest does not
remove it live. Patch it out and verify.

## 4. Deploy

**Flux applies the new role, not `deploy.sh`.** The entry carries `owner: flux`, so
`./scripts/deploy.sh --tags "<name>"` exits 5 (`DEPLOY_FLUX_OWNED`), `--dry-run` included.
To check the role before the merge, render it the way `promote` does:

```bash
uv run python scripts/renderer/homelab_render.py cluster --out out --role <name>
uv run python scripts/deploy_tools/promote.py check-bundle out
```

Once the PR merges, `promote` publishes the next bundle and Flux applies the tag. Its
*Publishing and promotion* and *Verification* sections in `docs/flux-deploy-plane.md` say
what gates the apply. Two limits specific to a *brand-new* service:

- The render proves the manifests are well-formed, not that the cluster admits them. Nothing
  before the merge verifies that a referenced PVC can be provisioned.
- A clean render says nothing about scheduling, PVC binding, probe or rollout behaviour.
  Those show only after Flux applies the tag. Gate it with
  `uv run python scripts/diagnostics/probe.py health <name>`.

An entry with the `owner` line deleted deploys through `./scripts/deploy.sh --tags "<name>"`.
The `deploy` skill covers that wrapper's exit codes and the `--check` / `--dry-run` / `prek`
split, and the rest of this section applies to such an entry.

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
denylist: k8s auto-deploy disarmed — stale (denied at origin but not in config: ['<name>'])
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
