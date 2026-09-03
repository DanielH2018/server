# netpol-baseline — default-deny NetworkPolicies for the cluster

Renders the baseline NetworkPolicy for both `homelab` and `observability`, plus a
per-workload override under `templates/networkpolicy-*.yaml.j2` for a service that needs a
tighter or looser allow-list than the baseline. Deploys no workload of its own.

## At a glance
- **Renders nothing runnable** — NetworkPolicy objects plus five probe Jobs
  (`netpol-probe*-job.yaml.j2`) that verify the policy actually fenced what it claims to.
- **Deploy tag:** `--tags "netpol-baseline"`. `k8s_autodeploy: true` — an image-only diff
  touches only the pinned probe image; the policies re-apply unchanged and the role hard-fails
  if the live exempt set has drifted from `netpol_baseline_exempt_workloads`.
- **`netpol_baseline_scope: namespace`** — the baseline selects every pod in the namespace
  EXCEPT one carrying `netpol-baseline-exempt` (a workload with its own, tighter policy). Set
  to `label` to scope to opt-in pods only; `netpol_baseline_enforced: false` disables
  enforcement entirely without deleting the rendered policy.
- **Observability namespace has its own levers** (`netpol_baseline_obs_enforced`,
  `netpol_baseline_obs_scope`, own node-CIDR list) — rolled out and back independently of
  `homelab`'s.

## Notable
- **Both boolean levers coerce oddly.** `netpol_baseline_enforced`/`_obs_enforced` go through
  `| bool` on both the template's `if` and the probe tasks' `when:`, so a typo like `fasle` is
  silently `False` on both sides: the allow-all body renders, the verifying probe skips, and
  the deploy reports green while nothing is fenced. `tasks/main.yml`'s first task asserts both
  values are literally `true`/`false` before anything renders, for exactly this reason.
- **The exempt-workload list is read as an exact set, live.** A name in
  `netpol_baseline_exempt_workloads` but absent from the cluster is a widening about to happen;
  a name in the cluster but missing from the list is a pod fenced by nothing. The gate exists
  because the two disagreed for ~16 hours during slice 4.5;
  `ansible/tests/k8s/test_netpol_baseline_labels.py` pins the same set against templates.
- **`netpol_baseline_node_cidrs` are `/32` host addresses on purpose** — the bridge IPs traffic
  arrives with when it carries a node IP (image pulls, hostPort, host crons). A `/16` here
  would silently cancel the whole policy.

## Editing
Per-workload policy: `templates/networkpolicy-<name>.yaml.j2`. Deploy:
`uv run ansible-playbook ansible/deploy.yml --tags "netpol-baseline"`.
