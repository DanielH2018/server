# Target architecture — design

This page is the durable overview of the homelab target-architecture migration. It says why
the migration exists, where each area ends up, and the order the slices run in. It records the
substance of the churn survey of 2026-10-11, whose rendering is pruned after 30 days.
[contracts.md](contracts.md) holds the interfaces every slice builds against, and this page
does not restate them. Nothing here is built at `70591c307`, and every slice is PLANNED.

## Why the migration exists

The 2026-10-11 survey read git history, GitHub issues and incident evidence over 90 days.
Nearly every cited issue is closed, so the evidence is a defect *class* that keeps recurring,
and the ranking weighs refiles, reopens and incidents above raw issue counts. It measured 3,386
commits on master, about 7% of them fix-type; 1,668 issues, 75% filed in the last 30 days and
99.8% labelled `claude`; and 42 red CI runs on master since 2026-07-13. The GitOps deployer
held 9,499 lines, against 1,928 when ADR-0010 was accepted, and `scripts/deploy_tools/land.py`
with its `land_lib/` held 6,209. Prometheus had 0 alert rules, and there was no Alertmanager.

### The four hotspots

1. **Cluster deploy plane** (`setup/gitops_deploy`, `scripts/deploy_tools`, `land.sh`): 309 +
   318 commits and about 375 issues counting `land.sh`. The deployer decides which part of a
   diff to apply. Its defect classes:
    - A. Deploy tags come from changed file paths, a lossy stand-in for the rendered output,
      so the choice is too narrow, too broad, or fires on a comment-only edit ([#2581](https://github.com/DanielH2018/server/issues/2581), [#2810](https://github.com/DanielH2018/server/issues/2810)).
    - B. The trigger is the commit range, not the desired state, and one path the deployer
      cannot place parks the whole range ([#2348](https://github.com/DanielH2018/server/issues/2348), [#4326](https://github.com/DanielH2018/server/issues/4326)).
    - C. Completion is read from the deployer's own markers, so it reports "settled" or
      deploys nothing and exits 0 ([#1537](https://github.com/DanielH2018/server/issues/1537), [#1813](https://github.com/DanielH2018/server/issues/1813)).
    - D. `kubectl apply` leaves stale Secret keys and orphaned objects behind.
    - E. A dirty tree, an untracked file or the tree lock parks the deployer ([#1155](https://github.com/DanielH2018/server/issues/1155)).
1. **Monitoring** (`k8s/monitor-bridge`, `k8s/uptime-kuma`, `probe.py`): 250 + 185 commits and
   205 issues. [#2076](https://github.com/DanielH2018/server/issues/2076) deleted all 107 Kuma
   monitors, and [#1803](https://github.com/DanielH2018/server/issues/1803) recorded 602
   rejected pushes and 35 false DOWNs. Its classes: D1, the Kuma database drifts from git; D2,
   verdicts travel by token-keyed push into a second stateful system; D3, plumbing fans out per
   check ([#3319](https://github.com/DanielH2018/server/issues/3319)); D4, queries fail open
   and go silently blind ([#371](https://github.com/DanielH2018/server/pull/371)); D5, the
   post-deploy gate's release records go stale ([#2902](https://github.com/DanielH2018/server/issues/2902)).
1. **Host plane** (`setup/k3s`, `host_vars`, the bring-up playbooks, the Pi): 264 + 291
   commits. Nothing declares an absent state. Stale units stay on the old host, `-e target`
   runs on the wrong machine, and a killed run leaves the dpkg lock held
   ([#929](https://github.com/DanielH2018/server/issues/929), [#1916](https://github.com/DanielH2018/server/issues/1916)).
   About 56 of the 74 line edits to the 12 most-edited keys in
   `ansible/roles/setup/k3s/defaults/main.yml` are cluster config, which ships only through
   the hand-run `ansible/k3s-bringup.yml`.
1. **Docs/facts guard** (`docs/facts.lock`, the `CLAUDE.md` size ceilings): 113 lock commits
   and 79 issues, 7 of them open, the most open of any bucket. It is the leading confirmed
   cause of red CI on master ([run 36485716039](https://github.com/DanielH2018/server/actions/runs/36485716039),
   [run 35729601483](https://github.com/DanielH2018/server/actions/runs/35729601483)), and a
   Renovate PR cannot satisfy it ([#4012](https://github.com/DanielH2018/server/issues/4012)).
   Over 60 days, 112 lock commits produced 6 prose fixes.

Three of the four fail the same way. The deployer pushes a diff range, monitoring pushes
heartbeats, and the lock stores a hash. Each feeds that computed delta into stateful machinery
and reads completion from its own markers. The host plane fails by omission: Ansible applies
what a role declares and never removes what the role stopped declaring. The target replaces
all four with declarative reconciliation against git, and removes Ansible. Each slice checks
its risks against [the recurring failure classes](../failure-classes.md). The survey excluded
`scripts/dev` on purpose: most of its 268 commits in 30 days are moves or folds.

## Target end state by area

### Cluster deploy plane: Flux, a CI-rendered OCI artifact and homelab-render

**Decision.** CI renders every manifest with `homelab-render` (C1), a Python + Jinja2 renderer
that keeps the 273 templates and reuses `scripts/lib/ansible_jinja_env.py`,
`scripts/deploy_tools/render_targets.py` and `ansible/filter_plugins/`. CI pushes the output
as an OCI artifact, a `promote` workflow moves the tag Flux follows (C2), and
kustomize-controller applies it server-side with prune and wait (C3, C4).

ADR-0010's premises have moved. Its decision logic grew from 1,928 to 9,499 lines, and the five
path narrowings exist "because a path moved while the rendered bytes did not"
([#2810](https://github.com/DanielH2018/server/issues/2810)). Flux decrypts age natively, and
daniel-box ran at 44% memory on 2026-10-11, not the ADR's 65%. Rejected: Argo CD (its SOPS
plugin puts the age key in repo-server), Kluctl in-cluster (CI no longer sees the deployed
bytes), and Kustomize, Helm or CUE as renderers (a rewrite of 21.6k template lines).

**Removes by construction** classes A to E. With them go the orphan-GC cron,
`ansible/roles/k8s/manifests/tasks/verify_secret_keys.yml`, positional ordering, the
`k8s_autodeploy` denylist (replaced by `suspend`), the CI poll, the five narrowings, range
parking, the owed ledgers and cross-role reach. `land.py` and `land_lib` shrink from about 6.2k
lines to about 200.

**Remains** as owned code: the health-gated rollback and hold, which move to `promote` and
`DEPLOY_HOLD`; the restart soak, which becomes a Job that `promote` waits on through the
`flux/soak` status (D5, C7); PVC snapshots, which become a per-revision Job; mutable-tag image
rebuilds, which move to CI; and the Discord alerts.

**Failure modes it introduces:** literal `${` and undefined placeholders, and two multi-line
secret keys that break raw substitution (all R2). A render bug that drops a file deletes live
objects under prune. Deploys depend on GHCR, Flux CRDs need upgrading, and the cutover rolls
the 59 `kubectl-rollout`-annotated Deployments once (D5).

### Monitoring: one alert plane on Prometheus and Alertmanager

**Decision.** Alert rules live in git, tested by `promtool test rules` and pint, and fire
through Alertmanager to Discord, email and Home Assistant mobile. Host timers write
node_exporter textfile metrics, and monitor-bridge becomes an exporter for what only it reads
(B2, WAN, GitOps, Healthchecks.io). blackbox_exporter runs the ingress probes, and the
loki-homelab ruler owns log rules. A `Watchdog` rule always fires to Healthchecks.io, so an
Alertmanager outage pages off-site. C7 holds the labels and the health gate.

Much of monitor-bridge (12.2k lines plus 12.8k of tests) reimplements Alertmanager built-ins:
its gates are `inhibit_rules` for paging, its `STARTUP_GRACE` and the streaks of the rows
that read Prometheus are `for:` and `keep_firing_for:`, and its `resendInterval` is
`repeat_interval`. The in-body streaks of four checks stay in the bridge, and the bridge
still skips the body of a row whose gate reads down, so that `checks/b2.py:b2_storage_usage` sends no
billed listing during a B2 cap breach (M6). Rejected: Gatus, which removes D1 but keeps D2
and D3, and hardening Kuma, which only guards D1.

**Removes by construction** D1 to D3. Deleted: the uptime-kuma role (4 images, 2 PVCs, the
540-line `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2`, 2 sync CronJobs);
`ansible/roles/k8s/monitor-bridge/files/gates.py`, `apply_startup_grace` in
`files/bridge/streaks.py`, the push path
and the check bodies that read Prometheus and Loki; 82 of the 83 push-token secrets in
`ansible/secret_rotation.yml`, with their rotation entries;
`ansible/roles/setup/common/tasks/kuma_check_timer.yml`; and `probe.py kuma-drift`. Of the 83,
52 carry the `monitor_bridge_` prefix and 44 belong to the bridge's own check rows. M3 retires
1, M5 retires 3 and M6 retires 78. The 83rd, `registry_gc_push_token`, goes with registry GC
in D5's wave D.

**Remains:** D4, with better tooling (promtool zero-series tests, pint's `promql/series`
check, `absent()`), and D5, a release-record bug fixed separately from this migration.

**Failure modes it introduces:** the Watchdog's `repeat_interval` must stay below the
Healthchecks.io period plus grace, and Alertmanager needs a small PVC to keep silences.

### Host plane: one package per host and system-upgrade-controller

**First, shrink the plane**, which needs no host tool. Longhorn, MetalLB, CoreDNS and the
RecurringJobs leave `setup/k3s` for Flux (H1), so a Longhorn bump stops being a hand apply.
Most cluster-reading crons become CronJobs, one family per PR, and a few stay on the host by
decision (H2). Two native features replace hand-written ones: Longhorn's `filesystem-trim`
RecurringJob and k3s `etcd-s3` snapshots to R2. Those snapshots exclude the server token. The
token stays out of band, as `docs/break-glass.md` item 2 records, and a daily comparison on
daniel-box reports drift (H2). H7's fresh join decides whether agents move to an agent-scoped
token (H6 Open question 4).

Registry garbage collection does not move. D5 retires it in wave D, because after D5.4 no
build pushes to the in-cluster registry. An in-cluster job that scales the registry's
Deployment would also be a second writer on a flux-owned object, which C4 forbids. Wave D
removes the GC's cron, script, Kuma tile, Healthchecks.io entry and push token together, so
M6 does not cut registry-gc's check over and leaves its tile and token to D5. If M6 deletes
Kuma before wave D, the tile goes with Kuma, and wave D drops that part of its PR.

**Decision.** What remains on each host ships as one `.deb` per host (C5, C6). nfpm builds it
from homelab-render's host output, a LAN builder signs it after verifying the commit's
signature (H3 Open question 1 decides which signature it trusts), and a `homelab-updater`
timer installs it from an apt repo served outside k3s. dpkg is the only mechanism the survey
found that removes config the repo stopped shipping, on stock Ubuntu 24.04 and without an
agent. system-upgrade-controller `Plan`s, reconciled by Flux,
upgrade k3s. The Pi keeps Docker ([ADR-0013](../adr/0013-daniel-pi-stays-on-docker.md)) with
one merged Compose project under a systemd unit. One `bootstrap-host.sh` replaces the three
bring-up playbooks, so a first install and every later update share one code path. The
runner-up is ansible-pull, which becomes the plan if H3's `Depends` test fails. Rejected: agent
tools on a 456 MB Pi (Salt, Puppet, Chef), pyinfra's push mode, sysext (a read-only `/usr` and
`/etc` break apt and k3s), bootc (a demo on Ubuntu), Ubuntu Core (snaps only), chezmoi, doco-cd
and Komodo.

**Removes by construction:** undeclared absent state for files and units; an unknown applied
commit, since the package version carries it; runs on the wrong machine, since each host pulls
only its own package; and a held dpkg lock, since apt runs locally. Deleted: ansible-core and
its collections; 595 files (about 25.2k YAML/Jinja lines) under `roles/setup` and
`roles/containers`, part of which moves to Flux; 290 lines of bring-up playbooks; the
local/ssh inventory model; every `has_X` teardown arm; and setup-drift.

**Remains:** non-file state (users, ufw, libvirt, LVM) needs one small reconciler per kind. A
hand edit is reverted on the next version, and `dpkg --verify` reports it in between. A k3s
config change restarts the API server, so it is gated to a window. Prior art is thin.

**Failure modes it introduces:** a failing postinst wedges every apt run, unattended-upgrades
included. The signing key is root-equivalent on every host, so only the LAN builder holds it.
C5's package rules bound both.

### Docs guard: generate or point, and retire facts.lock

**Decision.** Facts move into generated blocks, `docs/reference/`, `DECIDED` markers and
`--help` text, and `CLAUDE.md` keeps directives and pointers. `docs/facts.lock`, the verify and
reverify commands of `scripts/dev/fact_status.py` and the hash test are deleted; lint stays. A
deterministic, non-blocking CI notice lists the sections that cite atoms a PR moved, and size
gets one read-time budget plus a report. Root `CLAUDE.md`, 309 lines at `70591c307`, moves
toward Anthropic's guidance of under 200 lines. Rejected: a bot that regenerates the lock,
because a lock rewritten without reading the prose attests nothing.

**Removes by construction:** G1, a stored attestation that unrelated diffs invalidate (over 60
days, content atoms gave 3 prose fixes from 10 moves, and path atoms 3 from 27); G2, a per-file
size ratchet whose count a session raised instead of trimming
([#2982](https://github.com/DanielH2018/server/issues/2982)); and G4, generated output in the
deploying checkout, which goes with the cluster deploy plane. Deleted: 4,077 lines of lock
machinery and the `OVER_CEILING` exemptions that
`ansible/tests/_doc_size.py:recorded_count_problems` checks. Added: a diff-to-sections step of
about 100 lines, and a one-time rewrite of the 87 `CLAUDE.md` files.

**Remains:** G3, restated or uncited prose that the lock never saw. The most-read sections cite
nothing: monitor-bridge's `CLAUDE.md` at 216 reads and root `CLAUDE.md` at 166
([#4250](https://github.com/DanielH2018/server/issues/4250)). G5's rewrite addresses it, and the
notice is advisory, so a moved atom can still go unread.

## Combined end state

Ansible is gone, and git is the only write path. CI runs one renderer and produces two
artifacts: an OCI artifact that Flux reconciles into the cluster, and one signed `.deb` per
host that apt pulls. Both report the commit they applied (C6), so `land.sh` waits on facts
rather than markers. Prometheus rules own alerting, and generators own the facts docs state.
SOPS gains consumers outside Ansible: the cluster's substitution Secret and its SOPS-encrypted
Secret manifests (C3, produced as D3 decides), and each host's own age key. The Pi gets a SOPS
file of its own, encrypted to its own key, and stays off `secrets.yml`'s recipient list (H4).

**Net complexity.** Deleted: ansible-core, the 9,499-line deployer, about 6k `land` lines,
the `narrow_*` and reach scripts, about 25k lines of setup and containers roles, the bring-up
playbooks, about 100 Ansible-mechanics tests, the Kuma role and the bridge's check bodies, 83
push-token secrets and 4,077 lines of lock machinery. Added: the renderer (about 600 lines),
Flux with about 65 generated Kustomizations, a promote workflow (about 150 lines), 4 nfpm
specs, 5 shared units and three reconcilers, system-upgrade-controller, Alertmanager,
blackbox_exporter, rule tests, the diff notice, and a LAN package builder with its signing key.
Each addition is a standard tool or a small piece of owned code, and no language is added.

## ADR changes this migration needs

ADRs are never rewritten ([the superseding rule](../adr/index.md)): a superseding ADR marks the
old one `Superseded by ADR-NNNN`, and an amending ADR adds an "Amended by" note, as ADR-0017
did for ADR-0011. The owning slice takes the next free number. Approving any ADR is
**operator-only**.

- **Supersede [ADR-0010](../adr/0010-pull-based-gitops-over-argo-and-flux.md)** in D6
  (**operator-only**). Its three premises moved, as the deploy-plane section sets out.
- **Amend [ADR-0003](../adr/0003-sops-with-age-for-secrets-at-rest.md)** in D3 and H4
  (**operator-only**). Whichever slice lands first writes the amendment, and the other
  extends it (D3 Open question 1). Its decision decrypts at deploy time through
  `community.sops.sops_decrypt`. Afterwards the cluster's own age key and each host's own age
  key decrypt. The Pi becomes a recipient of its own host file only, and stays off
  `secrets.yml`'s list (H4). Adding a recipient to `ansible/.sops.yaml` is
  **operator-only**.
- **Amend [ADR-0019](../adr/0019-fact-support-stops-at-the-repo-store.md)** in G2
  (**operator-only**). ADR-0019 retires the lock only when `fact_status.py report --days 60`
  shows fewer than two actual moves across its content forms. Over 60 days the survey counted
  10 content-atom moves, 3 of them prose fixes. Both figures are at or above two, so that
  trigger does not fire.
- **ADRs this migration adds**, each **operator-only**: homelab-render with the Flux deploy
  plane, which may be the ADR that supersedes ADR-0010; one alert plane on Prometheus and
  Alertmanager, retiring Uptime Kuma (M6); and one package per host with
  system-upgrade-controller, replacing Ansible's host configuration. H3's `Depends` test
  decides between the package and ansible-pull, and H7 moves that design into `docs/adr/`.
- **Also affected**; the operator decides whether each needs an amendment
  (**operator-only**). [ADR-0011](../adr/0011-one-lock-serialises-every-deploy-path.md) and
  [ADR-0017](../adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md) lose `deploy.sh`
  and `gitops-deploy.service` as lock holders (D6, R4), though the crons that write the tree
  keep the tree lock. [ADR-0012](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md)'s
  rollout gate becomes Flux's `wait`, and its 180-second soak a Job that `promote` waits on
  (D5).
  [ADR-0013](../adr/0013-daniel-pi-stays-on-docker.md) drives the Pi over SSH with
  `-e target=daniel-pi`, which the package replaces (H5).
  [ADR-0001](../adr/0001-mkdocs-site-with-generated-reference.md) generates the reference
  pages from the Ansible tree, so H7 checks what the generators read afterwards.

## Sequencing across the areas

The dependency table in [contracts.md](contracts.md) is authoritative, including its PR-level
sub-edges. This sketch follows its slice-level edges:

```text
G1 ──▶ G2 ──▶ G5          G2 ──▶ G3 (optional)          G4 (independent)
R1 ──▶ R2, R3
M1 ──▶ M2, M3, M4, M5 ──▶ M6          M6's B-1e ──▶ D5's last stateless-leaf flip
D1 + R1 + R2's PRs 1–2 ──▶ D2 ──▶ D3 (+ all of R2), D4 ──▶ D5 (+ R3, M1, M2) ──▶ D6 ──▶ R4
D2 + D4 ──▶ H1.1a–H1.8 ──▶ H2 ──▶ D5 wave D          R4 ──▶ H1.9
R1 ──▶ H3 ──▶ H4 ──▶ H5          D2 + H1.1a–H1.8 + H4 + M1 ──▶ H6
H2 + H5 + H6 + R4 ──▶ H7
```

D2 and D3 interleave. D3.0 to D3.3 need only D2.1 to D2.6, and by default D2.7, the leaf's
flip, waits on D3.3, because the leaf's route needs the SOPS variable `domain` (C3). D4.0 lands
before D2.7. D2.6b ships the empty `SHARED_KINDS` and the publish census that D3.3a adds
`cluster-vars` to.

The docs guard goes first: it is independent, cheapest, and removes the leading confirmed
cause of red CI on master. R1 roots two areas, since D2 and H3 both need its renderer. D5
waits on M1 and M2 because the soak Job reads M2's pod-to-service mapping and the alerts M1
routes. D5's wave D waits on H2's family PRs, so each role's crons leave the host while
Ansible still owns the role. H6 waits on D2 and H1 because Flux reconciles its Plans and its
vendored CRD, and on M1 for its alert rules. The critical path is R1, R2, D2, D3, D5, D6, R4,
H7. It runs through H1 and H2 instead if an H2 family PR that D5's wave D waits on also needs
an H1 PR. H2 states which of its PRs do. It runs through the monitoring slices instead if
M6's B-1e lands after D5 reaches its last stateless leaf. M6's live health-gate drill needs
a tag that is not flux-owned (M6 Open question 4), so that flip waits on the drill.

## Slice index

| ID | Slice | Exit in one line |
|---|---|---|
| R1 | [Shadow render with zero diff](render/r1-shadow-render.md) | homelab-render matches `deploy.sh --dry-run`'s render dirs byte for byte, every role |
| R2 | [Secret placeholders and CI secret checks](render/r2-secret-placeholders.md) | Secrets render as placeholders that CI checks against `secrets.yml` key names |
| R3 | [Remove the pre-apply live reads](render/r3-remove-live-reads.md) | pi-peer-backup, n8n and authelia render without reading live state |
| R4 | [Delete the k8s Ansible path](render/r4-delete-k8s-ansible-path.md) | The manifests role's render, apply, prune and drain tasks are gone |
| D1 | [Flux installed, owning nothing](deploy/d1-flux-installed.md) | Flux runs, its memory is measured, and a no-op revision posts a status |
| D2 | [One stateless leaf end to end on production](deploy/d2-first-leaf.md) | A broken probe on the leaf triggers the rollback and the hold, while the old pod keeps serving |
| D3 | [Secrets through Flux](deploy/d3-secrets.md) | A dropped secret key disappears from the live Secret |
| D4 | [Ordering](deploy/d4-ordering.md) | An aborted policies-first change leaves its dependents unapplied |
| D5 | [Bulk move](deploy/d5-bulk-move.md) | Every role is flux-owned, and `promote` waits on the soak Job's `flux/soak` status |
| D6 | [Delete the old deploy path](deploy/d6-delete-old-path.md) | The deployer's k8s arms are gone, its host-plane arm stays until H7, and an ADR supersedes ADR-0010 |
| M1 | [Alertmanager and its watcher](monitoring/m1-alertmanager.md) | Alertmanager scaled to 0 pages through Healthchecks.io |
| M2 | [One bridge family to rules](monitoring/m2-first-bridge-family.md) | The workload and DaemonSet-floor checks are rules with zero-series tests |
| M3 | [One host timer to the textfile collector](monitoring/m3-textfile-timer.md) | disk-health alerts on a grace derived from its own schedule |
| M4 | [blackbox_exporter for the ingress probes](monitoring/m4-blackbox-ingress.md) | Ingress probes run alongside Kuma through one Sunday reboot |
| M5 | [loki-homelab ruler](monitoring/m5-loki-ruler.md) | Two of the bridge's three Loki checks are ruler rules, each selector matching a stream; `swallowed_verdicts` waits for M6 |
| M6 | [Switch the readers and retire Kuma](monitoring/m6-retire-kuma.md) | `probe.py health` reads `ALERTS`, and Kuma and the push tokens are gone |
| H1 | [Hand the cluster content to Flux](host/h1-cluster-config-to-flux.md) | A Longhorn bump lands with no hand apply |
| H2 | [Shrink the host plane](host/h2-shrink-host-plane.md) | The cluster-reading crons are CronJobs or native features, one family per PR |
| H3 | [Pi pilot](host/h3-pi-pilot.md) | A later version's added `Depends` installs, and a merge's SHA shows on the Pi |
| H4 | [Secrets unit; NUT on daniel-server first](host/h4-secrets-unit.md) | NUT runs from the package, and the credential store is 0600 |
| H5 | [Pi Compose](host/h5-pi-compose.md) | A Renovate bump lands on the Pi, and a removed service loses its container |
| H6 | [system-upgrade-controller for k3s](host/h6-k3s-suc.md) | A patch bump upgrades both nodes through Plans |
| H7 | [Remaining roles, then delete Ansible](host/h7-delete-ansible.md) | A fresh Pi rebuilds from `bootstrap-host.sh` alone |
| G1 | [Diff notice alongside the lock](docs-guard/g1-diff-notice.md) | 30 days of notices, counted against the prose edits they caused |
| G2 | [Retire the lock](docs-guard/g2-retire-lock.md) | A PR that moves a cited symbol goes green and carries the notice |
| G3 | [Notice dispatches the freshness reviewer](docs-guard/g3-freshness-reviewer.md) | (optional) The notice dispatches a reviewer and stays the check |
| G4 | [Drop the CI size test](docs-guard/g4-drop-size-test.md) | Size is one read-time budget plus a report |
| G5 | [Shrink root CLAUDE.md](docs-guard/g5-shrink-root-claude-md.md) | Mechanism prose sits in `.claude/rules/`, and tool output in `--help` |

## Open questions

1. **Flux's live memory.** Requests total 192Mi, and no usage figure is published. D1
   measures it.
1. **`managedFields` on real Secrets.** The read-only ServiceAccount cannot read Secrets, so in
   D3 the operator reads them with `sudo k3s kubectl` before the cutover (**operator-only**).
1. **Undefined placeholders.** Answered: an undefined variable fails rather than becoming an
   empty string. R2's round trip runs with `--strict-substitute`, and D3's baseline shows
   kustomize-controller v1.9.6 substituting strictly by default. R2's PR 1 red-proofs it for
   the pinned binary.
1. **Where `domain` comes from before D3.** C3's default makes D2.7, the leaf's flip, wait on
   D3.3, so the leaf's route reads `domain` from `homelab-cluster-vars`. The operator may
   instead rule that `domain` is published in plaintext, which lets D2 finish before D3. The
   operator rules before D2.7 (**operator-only**).
1. **Three smaller design points.** Whether hashing SOPS ciphertext for restart checksums
   over-triggers when sops re-encrypts unchanged values (D3). How the Longhorn pre-apply
   snapshot runs once per revision (D5). Whether `deploy_ui` retires with the deployer (D6).
1. **The `Depends` question.** Whether the updater's `apt-get install` installs a newly added
   `Depends` cleanly on every host. H3 tests it first. If it fails, ansible-pull becomes the
   host plan and H4 to H7 are revised.
1. **Reconciler size.** Whether the reconcilers for ufw, libvirt and users stay small enough
   to justify a second mechanism beside dpkg. Each H-slice that adds one reports its size.
1. **The host census.** A research agent counted 595 files, about 25.2k lines, 114 setup tests
   and about 48 schedules, and nobody validated them. H2 and H7 re-measure them.
1. **k3s upgrade order.** Settled: H6 upgrades the server first, as the version skew policy
   and `docs/k3s-upgrade.md` require. The survey's H6 summary said the agent first, and H7
   corrects that text when the host design moves into `docs/adr/`.
1. **When the deploy direction is approved.** D1 installs Flux, which ADR-0010 deferred,
   although ADR-0010's own evaluation recommended adding Flux in narrow slices. The operator
   rules before D1 merges, on a superseding ADR in `Proposed` status or by confirming that D1
   fits ADR-0010 (**operator-only**).
1. **The hand fast-forward after a park.** At `70591c307`, `docs/gitops-pipeline.md` *When a
   tick parks* and `deploy_remediation.py:broad_remediation` both tell the operator to run
   `git merge --ff-only origin/master` before the playbook. The docstring of
   `land_tags.py:self_applied_command` says that once local equals origin, every later tick
   is a noop. A fast-forward to origin therefore strands every commit merged after the
   parking commit. Until the pipeline doc and the remediation text fast-forward to the
   parking commit instead, any slice that edits a bring-up playbook bounds its own hand
   fast-forward to its squash SHA after a `git rev-list HEAD..<sha>` check, as R4's PR 6
   does. Who makes the general fix, and when, is open.
1. **An installable `scripts/` package**
   ([#4435](https://github.com/DanielH2018/server/issues/4435)). Resolution: decide it before
   R1 fixes the renderer's import path, or defer it until after D6, so that the path changes
   once.

## Review log

### Round 1 — integration review (X-series)

Raised against this page: X-08 and X-20. Both are fixed, and no part is refuted.

- **X-08 (high).** The D2 row said "on stage", and no stage cluster exists. Fixed: the row
  reads "on production", as D2's body does. contracts.md C8 forbids a stage gate and names
  three substitutes, and the slices' own steps change in R4, D6 and H2.
- **X-20 (medium).** This page contradicted six slices. Each fix follows the slice, and none
  alters an operator decision:
  - *Soak.* D5.3 makes the soak a Job behind `flux/soak` and adds no rule for it. The
    deploy-plane *Remains* paragraph, the ADR-0012 bullet, the sequencing paragraph and the D5
    row now say so.
  - *Server token.* H6 Open question 4 rejects putting the server token in SOPS, because
    `docs/break-glass.md` item 2 keeps it out of band. The host-plane paragraph now says the
    token stays out of band, and H7's fresh join decides on an agent-scoped token.
  - *D6 row.* D6's goal keeps the deployer's setup arm until H7. The row now says the k8s
    arms go and the host-plane arm stays.
  - *R3 row.* R3 removes authelia's digest read too. The row names it, and the contracts.md
    row is renamed "Remove the pre-apply live reads" (R3 Open question 1).
  - *Pi recipient.* H4 keeps the Pi off `secrets.yml`'s recipient list and makes it a
    recipient of its own host file only. The combined end state and the ADR-0003 bullet now
    say so.

The integration review also changed three things here that follow from contracts.md:

- The sequencing sketch and the critical path follow the new index edges: D2 on R2, the D2
  and D3 interleave, H2 on H1.1a to H1.8, D5's wave D on H2, and H6 on H1 and M1.
- Registry garbage collection retires in D5 and does not move in H2 (X-06).
- Two open questions are answered (undefined placeholders, the k3s upgrade order), and one
  is added for the operator: where `domain` comes from before D3 (X-01).

### Round 2 — integration review (X2-series)

Raised against this page: X2-13. It is fixed, and no part is refuted. X2-08 and X2-11 changed
contracts.md, and this page follows them.

- **X2-13 (medium).** D5's wave D and M6's batch B6 both retired registry-gc's cron check,
  Kuma tile and push token. Confirmed: D5's wave D registry bullet removes all three, and M6's
  B6 row lists registry-gc while M6's Phase A count includes its token. The fix takes the
  first of the two remedies X2-13 offers, because X-06 already made D5 the GC's one owner.
  M6 drops registry-gc from B6 and leaves its tile and token to D5. The monitoring census now
  reads 82 of the 83 push tokens: 1 for M3, 3 for M5 and 78 for M6, with the 83rd going in
  D5's wave D. The host-plane paragraph says what happens to the tile if M6 deletes Kuma
  first. The net-complexity line keeps 83, because it counts the whole migration. Measured
  at `70591c307`: `grep -cE '^ *[a-z0-9_]*push_token:' ansible/secret_rotation.yml` prints
  83, the same count with the `monitor_bridge_` prefix prints 52, and
  `registry_gc_push_token` is one of the other 31. M6's own counts change in M6.

The round also changed three things here that follow from contracts.md:

- The sequencing sketch gains M6's B-1e before D5's last stateless-leaf flip (X2-08), and
  the critical path names the monitoring slices for when the drill lands late. D2 waits on
  R2's PRs 1 and 2 only, and D3 waits on all of R2.
- D4.3a no longer exists (D4-R10-01). The sequencing paragraph names D2.6b as the PR that
  ships the empty `SHARED_KINDS` and the census.
- The M5 row follows M5 Open question 4: two of the three Loki checks become ruler rules,
  and `swallowed_verdicts` waits for M6 (X2-11).

### Cross-slice follow-up (2026-10-12)

- Cross-slice note from M6: *Monitoring* now deletes only `apply_startup_grace` from
  `files/bridge/streaks.py`, which keeps `State`, `down_streak` and the probe caches that
  `bridge/sources.py`, `checks/cloudflare_ips.py` and `checks/healthchecks.py` import at
  `70591c307`. Only `STARTUP_GRACE` and the Prometheus rows' streaks become `for:`, and the
  bridge keeps skipping a gated row's body.
- Cross-slice note from R4: Open questions gains *The hand fast-forward after a park*.
  `docs/gitops-pipeline.md` describes the pipeline at `70591c307` and is not edited here.
- Open questions gains [#4435](https://github.com/DanielH2018/server/issues/4435), the installable `scripts/` package.
