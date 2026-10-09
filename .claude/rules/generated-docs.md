---
paths:
  - "docs/**"
  - "scripts/docs/**"
  - "ansible/roles/**/CLAUDE.md"
---

# Generated docs fragments and `## At a glance` blocks

The hand-written pages under `docs/` transclude their fact tables — a `--8<--` include pulling
in a file `scripts/docs/gen_doc_fragments.py` writes under `docs/assets/generated/fragments/`.
The prose stays hand-written; the tunables beside it are re-read from the tree.

**Changing one of those tunables rewrites its fragment at commit time.** The
`regen-doc-fragments` prek hook runs `gen_doc_fragments.py --fix` on any commit that touches a
source the generator reads. It rewrites the stale fragment and fails once like a formatter, so
stage the rewritten fragment and commit again. `FRAGMENTS` in that generator lists every
tunable it reads. A commit that skipped prek fails
`test_every_committed_fragment_matches_what_the_generator_writes_now` in CI; the repair is
`uv run python scripts/docs/gen_doc_fragments.py` and a commit of what it writes.

**A role `CLAUDE.md`'s `## At a glance` block is generated the same way, in place.**
`scripts/docs/gen_role_glance.py` writes one field set per role shape between two
`generated_from` markers under that heading: a deployed k8s role's deploy tag, image
repositories, route, claims with the Longhorn backup tier of each, and auto-deploy stance; a
setup role's applying playbook and tag, crons and timers; a Pi compose role's deploy tag,
image repositories, `containers_list` facts and `common_config_changed` wiring. The prose below the markers stays hand-written. Changing a
role's defaults, templates, tasks, playbook entry or `containers_list` entry — or, for a
claim's tier, its StorageClass or the k3s role's `k3s_longhorn_*_volumes` lists — moves the
block. The `regen-role-glance` prek hook runs `gen_role_glance.py --fix` on that commit. It
rewrites the block and fails once like a formatter, so stage the rewritten doc and commit
again. `scripts/docs/tests/test_gen_role_glance.py` is the CI backstop for a commit that
skipped the hook. The docs-refresh cron does not run the generator: a write under
`ansible/roles/` is outside the paths the cron stages, so it would leave the primary checkout
dirty.

**What the cron DOES stage is a closed list, and a generator outside it dirties the tree.**
Three paths: `docs/reference/`, `docs/assets/generated/`, and `scripts/dev/pytest_shard_weights.json`
(the test shard-weights table, recorded by `pytest_shard.py --record-missing` since #2274).
Adding a fourth means editing the cron's `git add`, the porcelain check in its
`restore_generated_tree`, and `ansible/tests/setup/test_docs_refresh_records_shard_weights.py`,
which holds the first two against each other. A path written but not staged parks the GitOps
deployer, which reads any porcelain output on the primary checkout as dirty.

**Why this one gate is not left to the cron**, when a stale `docs/reference/` page is. A
reference page carries a `generated_at` banner, so a stale one announces itself on the page.
A fragment is spliced into someone else's prose and carries no stamp a reader can see, so
nothing but `test_every_committed_fragment_matches_what_the_generator_writes_now` would catch
it drifting. The asymmetry is the point, not an oversight.

Two paths are safe and stay safe: the weekly secret-rotate cron commits `--no-verify`, and a
rotation moves `last_rotated` rather than a tier count, so the fragment does not move either
way; the docs-refresh cron regenerates before it commits, so its own hooks pass.
