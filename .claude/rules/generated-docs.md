---
paths:
  - "docs/**"
  - "scripts/docs/**"
  - "k8s/roles/**/CLAUDE.md"
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

**A value stated inside a sentence is rewritten at commit time too.** A fragment is a whole
table. For one number in hand-written prose, `scripts/docs/doc_values.py` holds a row: the
doc, a regex that captures the stated value, and a reader of the file the value comes from.
The `regen-doc-values` prek hook runs that script on a commit that touches a row's doc or
source. It rewrites the value and fails once, so read the sentence around it, stage the doc
and commit again. The hook corrects a count, never the list written beside it. A row whose
sentence was reworded fails until the row or the sentence changes. To add a row, extend
`VALUES` and the hook's `files` pattern; `tests/repo/test_doc_values_hook.py` holds the
pattern against the rows. A value Renovate bumps gets no row, and neither does a path the
docs-refresh cron stages.

**A role `CLAUDE.md`'s `## At a glance` block is generated the same way, in place.**
`scripts/docs/gen_role_glance.py` writes one field set between two `generated_from` markers
under that heading: a k8s role's **Applied by** line and deploy tag, image repositories, route,
claims with the Longhorn backup tier of each, and auto-deploy stance. The prose below the
markers stays hand-written. Changing a role's defaults, templates or `containers_list` entry —
or, for a claim's tier, its StorageClass or the `longhorn-reconcile` role's
`k3s_longhorn_*_volumes` lists — moves the block. The `regen-role-glance` prek hook runs
`gen_role_glance.py --fix` on that commit. It rewrites the block and fails once like a
formatter, so stage the rewritten doc and commit again. `scripts/docs/tests/test_gen_role_glance.py` is the CI backstop for a commit that
skipped the hook. The docs-refresh cron does not run the generator: a write under
`k8s/roles/` is outside the paths the cron stages, so it would leave the primary checkout
dirty.

**What the cron DOES stage is a closed list, and a generator outside it dirties the tree.**
Four paths: `docs/reference/`, `docs/assets/generated/`, `scripts/dev/pytest_shard_weights.json`
(the test shard-weights table, which `pytest_shard.py --record-missing` records and
`--refresh-drifted` re-measures where it drifted), and
`scripts/dev/pytest_shard_runner_weights.json` (the runner-seconds table the CI split packs on,
which `--refresh-runner` refreshes from CI's own logs).
Adding a fifth means editing the cron's `git add`, the porcelain check in its
`restore_generated_tree`, and `hosts/tests/test_docs_refresh_records_shard_weights.py`,
which holds the first two against each other. A path written but not staged parks
`homelab-checkout-sync`, which reads any porcelain output on the primary checkout as dirty.

**Why this one gate is not left to the cron**, when a stale `docs/reference/` page is. A
reference page carries a `generated_at` banner, so a stale one announces itself on the page.
A fragment is spliced into someone else's prose and carries no stamp a reader can see, so
nothing but `test_every_committed_fragment_matches_what_the_generator_writes_now` would catch
it drifting. The asymmetry is the point, not an oversight.

One path is safe and stays safe: the docs-refresh cron regenerates before it commits, so its
own hooks pass.
