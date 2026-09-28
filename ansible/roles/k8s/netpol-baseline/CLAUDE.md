# netpol-baseline — default-deny NetworkPolicies for the cluster

Renders the baseline NetworkPolicy for both `homelab` and `observability`, plus a
per-workload override under `templates/networkpolicy-*.yaml.j2` for a service that needs a
tighter or looser allow-list than the baseline. Deploys no workload of its own.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "netpol-baseline"`
- **Image:** `alpine` (`netpol_baseline_probe_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **Renders nothing runnable** — NetworkPolicy objects plus five probe Jobs
  (`netpol-probe*-job.yaml.j2`) that verify the policy actually fenced what it claims to.
- **Auto-deploy-eligible because** an image-only diff touches only the pinned probe image; the policies re-apply unchanged and the role hard-fails
  if the live exempt set has drifted from `netpol_baseline_exempt_workloads`.
- **`netpol_baseline_scope: namespace`** — the baseline selects every pod in the namespace
  EXCEPT one carrying `netpol-baseline-exempt` (a workload with its own, tighter policy). Set
  to `label` to scope to opt-in pods only; `netpol_baseline_enforced: false` disables
  enforcement entirely without deleting the rendered policy.
- **Observability namespace has its own levers** (`netpol_baseline_obs_enforced`,
  `netpol_baseline_obs_scope`, own node-CIDR list) — rolled out and back independently of
  `homelab`'s.

## Notable
- **Both boolean levers coerce oddly.** They go through `| bool` on both the template `if`
  and the probe tasks' `when:`, so a typo like `fasle` is silently `False` on both sides: the
  allow-all body renders, the verifying probe skips, and the deploy reports green while
  nothing is fenced. `tasks/main.yml`'s first task asserts both are literally `true`/`false`.
- **The exempt-workload list is read as an exact set, live** — a name absent from the cluster
  is a widening about to happen; a cluster workload missing from the list is fenced by
  nothing. The two disagreed for ~16 hours during slice 4.5;
  `ansible/tests/k8s/test_netpol_baseline_labels.py` pins the set against templates now.
- **`netpol_baseline_node_cidrs` are `/32` host addresses on purpose** — a `/16` here would
  silently cancel the whole policy.

## Where a per-workload policy lives

A per-workload policy has two homes. What the policy reads, and what the workload's deploy
needs, decides which one.

- **In this role**, when the policy reads one of this role's variables: the
  `netpol_baseline_enforced` lever or `netpol_baseline_node_cidrs`. Role defaults are
  role-scoped, so neither resolves in another role. Every `templates/networkpolicy-*.yaml.j2`
  file here reads one of them.
- **In the workload's own role**, when the workload's deploy depends on the policy:
  - A workload in `netpol_baseline_exempt_workloads` keeps its complete fence beside it:
    headlamp, n8n, registry, prowlarr's flaresolverr and karakeep's chrome. Exempt, it is
    fenced by nothing else.
  - A backend that the pod's startup waits on ships with the pod. The precedents are
    authelia's session store (#1609) and the scrutiny and karakeep backends (#1620).
  - The additive caller rules of the *arr stack and qbittorrent live with the workload they
    admit callers to.

**A hand `--tags <svc>` deploy does not apply that service's policy when the policy lives
here.** When a change needs a policy change and a workload change together, deploy both
tags, workload role first and netpol-baseline last. `docs/networkpolicy-slice-answers.md`
records why that order matters. A new backend the pod waits on goes in the workload's role
instead, so one tag carries both.
