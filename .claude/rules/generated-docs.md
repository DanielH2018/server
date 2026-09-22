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

**Changing one of those tunables fails CI until you regenerate.** Run
`uv run python scripts/docs/gen_doc_fragments.py` and commit what it writes, in the same PR.
`FRAGMENTS` in that generator lists every tunable it reads.

**A role `CLAUDE.md`'s `## At a glance` block is generated the same way, in place.**
`scripts/docs/gen_role_glance.py` writes one field set per role shape between two
`generated_from` markers under that heading: a deployed k8s role's deploy tag, image
repositories, route, claims with the Longhorn backup tier of each, and auto-deploy stance; a
setup role's applying playbook and tag, crons and timers; a Pi compose role's deploy tag,
image repositories, `containers_list` facts, `meta/deps.yml` ordering and
`common_config_changed` wiring. The prose below the markers stays hand-written. Changing a
role's defaults, templates, tasks, playbook entry or `containers_list` entry — or, for a
claim's tier, its StorageClass or the k3s role's `k3s_longhorn_*_volumes` lists — fails
`scripts/docs/tests/test_gen_role_glance.py` until you re-run the generator and commit the
block. The docs-refresh cron does not run it: a write under `ansible/roles/` is outside the
paths the cron stages, so it would leave the primary checkout dirty.

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
