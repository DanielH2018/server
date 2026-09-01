# Archive

Superseded planning documents kept for history — the work they describe has already shipped,
was mothballed, or has been superseded by a design that stayed live. History follows via
`git mv`; content is unedited except for cross-links updated to the new path.

A programme with more than a couple of documents gets a subdirectory named for it, and the
shared filename prefix moves onto that directory — `networkpolicy/slice5-plan.md`, not
`networkpolicy-slice5-plan.md`. The four here are `k3s-migration/`, `networkpolicy/`,
`zero-downtime/` and `docs-ui-and-adrs/`.

A document belongs here once its decision lives somewhere a reader will actually find:
an ADR, a runbook, or a `# DECIDED:` marker at the line it governs. The *Decision recorded
in* column is that pointer, and an empty one means the work shipped without a decision worth
recording rather than that nobody looked.

| Moved from | Landed | Live documentation now | Decision recorded in |
|---|---|---|---|
| `k3s-migration/` (16 files) | k3s migration completed 2026-08-14 (`CLAUDE.md:4`) | repo-root `README.md`, `CLAUDE.md` | [ADR-0002](../adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md), [ADR-0013](../adr/0013-daniel-pi-stays-on-docker.md) |
| `networkpolicy/` (6 files) | slices 2–5 deployed and enforcing 2026-08-17 through 2026-08-20 (`docs/networkpolicy-default-deny.md:3`) | `docs/networkpolicy-default-deny.md` | [ADR-0009](../adr/0009-networkpolicy-default-deny-ingress.md) |
| `zero-downtime/design.md` | the rollout gate shipped; uv run `probe.py health` is the gate it specified | ADR-0012; `scripts/dev/measure_rollout_gap.py` grades the claims | [ADR-0012](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) |
| `zero-downtime/plan-1.md` | Task 1–2 complete, Task 3 steps 1–11 complete, per the plan's own execution-status note | ADR-0012 | [ADR-0012](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) |
| `zero-downtime/plan-2.md` | Pi-hole redundancy shipped — the `pihole-2` Deployment has existed since 2026-08-16 and both it and `pihole` read 1/1 | `ansible/roles/k8s/pihole/tasks/main.yml:24-33`, which states the one unfinished item | [ADR-0012](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) |
| `zero-downtime/plan-3.md` | readiness-probe follow-up to slice 1; slice 1 shipped, scope reduced 2026-08-16 | ADR-0012 | [ADR-0012](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) |
| `zero-downtime/baseline.md` | the measurement it records is taken; it dated itself to the pre-gate cluster | `scripts/dev/measure_rollout_gap.py` re-measures on demand | [ADR-0012](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) |
| `docs-ui-and-adrs/design.md` | the site is built, served and cron-refreshed | `docs/index.md` — the site itself | [ADR-0001](../adr/0001-mkdocs-site-with-generated-reference.md) |
| `docs-ui-and-adrs/plan-1.md` | complete — MkDocs site and generated reference pages, PR #416 | `docs/index.md` | [ADR-0001](../adr/0001-mkdocs-site-with-generated-reference.md) |
| `docs-ui-and-adrs/plan-2.md` | Tasks 1–4 complete — ADR format, link test, 14 backfilled records, scoped Vale gate, PR #417. **Task 5 was not done**, and is extracted to `docs-ui-and-adrs/diagrams-plan.md` in this archive | `docs/adr/index.md` | [ADR-0001](../adr/0001-mkdocs-site-with-generated-reference.md) |
| `docs-ui-and-adrs/plan-3.md` | complete — generated scripts reference, GitOps and deploy operator pages, PR #418 | `docs/deploying.md`, `docs/gitops-pipeline.md` | [ADR-0001](../adr/0001-mkdocs-site-with-generated-reference.md) |
| `host-python-314-plan.md` | complete — all 7 tasks executed and verified 2026-08-16, PR #239 (the plan's own status line) | `CLAUDE.md` → *Python & Tests* | — |
| `daniel-box-handoff.md` | historical — superseded 2026-08-14 by the k3s migration (marked in the file itself) | repo-root `README.md` | [ADR-0002](../adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md) |
| `happy-selfhost-spec.md` | mothballed 2026-07-19, no longer executable as written as of 2026-08-14 (marked in the file itself) | none — abandoned, not shipped | — |
| `docs-ui-and-adrs/diagrams-plan.md` | never started — Task 5 of the docs-UI programme, extracted live and left there. The tool choice it rested on is now a record; the six implementation steps are not | none — the pipeline does not exist | [ADR-0015](../adr/0015-d2-for-hand-authored-diagrams.md) |
| `ubuntu-24.04-upgrade.md` | both hosts upgraded 2026-06-05; the file's own header says not to follow it as instructions | — | — |
| `parallel-session-git-ci-design.md` | design approved for planning; the practice it describes is now documented directly | `CLAUDE.md` → *Parallel Claude Sessions* | — |

`docs/superpowers/plans/` and `docs/superpowers/ledgers/` are **not** in this archive: that
directory is gitignored (`.gitignore:6`, see commit `eadfdd57`) and untracked, so
it does not exist in this worktree to move. `docs/superpowers/specs/` stays where it is —
`PLANS.md:3` names it the authoritative rationale home.

`docs/networkpolicy-default-deny.md` stays live: its slices shipped, but the document
describes the enforced end state rather than the plan to reach it, so it reads as current
documentation and not as history.
