# Archive

Superseded planning documents kept for history — the work they describe has already shipped,
was mothballed, or has been superseded by a design that stayed live. History follows via
`git mv`; content is unedited except for cross-links updated to the new path.

| Moved from | Landed | Live documentation now | Decision recorded in |
|---|---|---|---|
| `k3s-migration/` (16 files) | k3s migration completed 2026-08-14 (`CLAUDE.md:4`) | repo-root `README.md`, `CLAUDE.md` | [ADR-0002](../adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md), [ADR-0013](../adr/0013-daniel-pi-stays-on-docker.md) |
| `networkpolicy-default-deny-plan.md`, `-slice2/3/4/45/5-plan.md` | slices 2–5 deployed and enforcing 2026-08-17 through 2026-08-20 (`docs/networkpolicy-default-deny.md:3`) | `docs/networkpolicy-default-deny.md` | [ADR-0009](../adr/0009-networkpolicy-default-deny-ingress.md) |
| `zero-downtime-deploys-plan-1.md` | Task 1–2 complete, Task 3 steps 1–11 complete, per the plan's own execution-status note | `docs/zero-downtime-deploys-design.md` | [ADR-0012](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) |
| `zero-downtime-deploys-plan-3.md` | readiness-probe follow-up to slice 1; slice 1 shipped, scope reduced 2026-08-16 (`docs/zero-downtime-deploys-design.md:4`) | `docs/zero-downtime-deploys-design.md`, `docs/zero-downtime-deploys-plan-2.md` | [ADR-0012](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) |
| `host-python-314-plan.md` | complete — all 7 tasks executed and verified 2026-08-16, PR #239 (the plan's own status line) | `CLAUDE.md` → *Python & Tests* | — |
| `daniel-box-handoff.md` | historical — superseded 2026-08-14 by the k3s migration (marked in the file itself) | repo-root `README.md` | [ADR-0002](../adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md) |
| `happy-selfhost-spec.md` | mothballed 2026-07-19, no longer executable as written as of 2026-08-14 (marked in the file itself) | none — abandoned, not shipped | — |
| `parallel-session-git-ci-design.md` | design approved for planning; the practice it describes is now documented directly | `CLAUDE.md` → *Parallel Claude Sessions* | — |

`docs/superpowers/plans/` and `docs/superpowers/ledgers/` are **not** in this archive: that
directory is gitignored (`.gitignore:6`, see commit `eadfdd57`) and untracked, so
it does not exist in this worktree to move. `docs/superpowers/specs/` stays where it is —
`PLANS.md:3` names it the authoritative rationale home. `docs/networkpolicy-default-deny.md` and
`docs/zero-downtime-deploys-plan-2.md`/`-design.md` also stay: they are the cited specs of
record, and the Pi-hole redundancy work in the zero-downtime pair is unshipped.
