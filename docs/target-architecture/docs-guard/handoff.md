# Docs guard — handoff of the open work

This page hands the rest of the facts-and-memory plan to another session. It states what
is done as of 2026-11-15, what is open, the order to take it in, and what the sweep
and the session after it taught. Items 1 to 3 landed on 2026-11-15, so the next session
starts at item 4. Delete this page in the PR that closes the last open item.

[ADR-0031](../../adr/0031-each-kind-of-fact-has-one-stateless-detector.md) holds the
decision, and each slice plan beside this page holds its own record. Read the ADR and
`.claude/rules/facts.md` before any of the work below.

## Where the plan stands

- [G6](g6-repair-audit-defects.md), [G8](g8-memory-resolver-and-replica.md) and
  [G10](g10-value-rows.md) are done. The operator declined G10's step 3 on 2026-11-14.
- [G9](g9-blocking-gate.md) stopped at its measurement on 2026-11-13. The diff notice
  stays advisory.
- [G7](g7-removed-names.md) has steps 1 to 4 done. Step 5 is open.
- [G11](g11-binding-sweep.md) has steps 1 and 2 done. Step 3 is open.

## Open work, in order

Take the items in this order. Each names the effort it needs from an Opus session.

1. **The value rows the sweep owed.** Done in
   [#8812](https://github.com/DanielH2018/server/pull/8812). The rows are in
   `scripts/docs/doc_values_lib/`. Worktree-cleanup's "four-layer ladder" got no row:
   `scripts/lib/worktrees.py:LOCAL_LAYERS` names three layers, and no constant names the
   fourth, the forge read.
2. **Mask the generated block in `has_prose`.** Done in
   [#8815](https://github.com/DanielH2018/server/pull/8815), which closed
   [#8787](https://github.com/DanielH2018/server/issues/8787).
3. **The sections the sweep kept back.** Done in
   [#8817](https://github.com/DanielH2018/server/pull/8817). The PR body lists what each
   sentence is bound to and the three `docker.md` sentences it corrected.
4. **G11 step 3.** High effort, solo. Rewrite `.claude/rules/facts.md` to the end-state
   model in the G11 plan's design, and replace tiers 2 and 3 of
   `.claude/agents/homelab-docs-freshness-reviewer.md` with a read of the sections that
   `fact_status.py report` ranks as loaded most and cited least. That agent's four
   uncited sections are bound in the same change. `facts.md` loads with every
   instruction file, so a wrong sentence there reaches every session.
   Three things to carry into the rewrite:
   - Keep the generated-block case in `facts.md#Commands`. #8815 added it to the list of
     sections the report does not rank.
   - The change edits a file under `.claude/agents/`, so the server lander refuses it.
     Run `probe.py lander`, then land it by hand with `land.sh`.
   - Re-paste any eval case that quotes an edited `facts.md` section, then run
     `evals/tests/test_eval_cases.py`.
5. **The pilot's comparison, due 2026-12-14.** High effort. Count the notice rows that
   the root `CLAUDE.md`'s eight content citations drew in the 30 days after
   [#8707](https://github.com/DanielH2018/server/pull/8707), and grade each unedited
   row as a false sentence or not. G9 measured 0.29 unedited rows per content citation,
   and 6% of them left a false sentence. The sweep's citations can be measured the same
   way from 2026-12-15.
6. **G7 step 5.** High effort. The operator ruled on 2026-11-15, and
   [G7's *Operator ruling*](g7-removed-names.md) holds the conditions. Only the comment
   class is a candidate to block; the live-doc class stays advice. Move the replay into
   `fact_status.py report`, measure 2026-11-15 to 2026-12-14, and have a session other
   than the one that built the replay grade at least 50 comment rows. Promote the class
   only if fewer than one row in five is a false alarm; otherwise close the step with both
   classes as advice. Take it in the same session as item 5, from 2026-12-15.
7. **The `ansible/` leftovers in comments.** Low effort, any time, independent of item 6.
   The advice on master listed 53 removed names in 74 places, and 43 of the names are
   `ansible/` paths the tree rename left in comments. Run `fact_status.py mv` once per
   moved path, with `--dry-run` first.

Two things are deliberately not on this list. ADR-0031 defers an outbox for memories
written on a replica host until refused writes are counted. Two directive sections cite
nothing by design: "A check that reaches over a network measures its transport first"
in `.claude/rules/python-layout.md`, and "Never print a secret value" in
`.claude/rules/k8s-templates.md`.

## Decisions that stay with the operator

- What the second window's grade means, if it lands near the bar. The ruling on G7 step
  5 sets the bar at 20% and names what happens on either side of it.
- What the 2026-12-14 number means for the citations the sweep added.
- Whether `.claude/rules/docker.md` should allow named volumes again. #8817 removed the
  allowance, because `hosts/lib/tests/_compose_tree.py:bind_problems` refuses every
  volume that is not a bind. Restoring it means loosening that census as well.
- The project-board sync for #8787. The `gh` token lacks the `read:project` scope. Once
  the operator grants it, `findings.py sync-board 8787` repairs the board.
- Any edit to the memory store. daniel-box holds the store, and daniel-server's copy is
  read-only by design, so a session on daniel-server cannot write a memory.

## What the sweep taught

- **A batch's own correction can be wrong.** In at least five of the eighteen batches
  the separate reviewer found a defect that the fix round repaired. One batch wrote
  that every landing under `DEPLOY_HOLD` merges, which is false for a pin bump. Keep
  the review phase on any batch that rewrites a sentence.
- **Two batches that cite one test conflict at merge.** Each writes its `# fact:`
  comment at the top of the same test function. The home-automation batch and the Home
  Assistant skills batch collided this way in
  `scripts/home_assistant/tests/test_ha_state_checks.py`. Group batches so that docs
  held by one test directory share a batch, or rebase the second PR and keep every
  comment.
- **A PR that touches the lander's own surface needs a hand landing.** The server
  lander refuses a PR that edits a file under `.claude/agents/`, a test under
  `scripts/deploy_tools/tests/`, or the claude-code host component. Run
  `probe.py lander` first, then land with `land.sh`.
- **A full set of landing slots reads as `needs-input` on daniel-box.**
  [#8797](https://github.com/DanielH2018/server/issues/8797) holds the defect. The PR
  is ready, and a hand landing finishes it.
- **A landing that merges and then ends `refused` graded nothing.** It happens when a
  landing module changes on master mid-landing. Re-run `land.sh --pr <n> --fresh
  --detach` for the verdict.
- **A landing refuses a checkout whose landing code is older than master's.** It refuses
  before it merges, so the PR stays open, and a fix can still go onto its branch. Re-run
  the same command with `--fresh`.
- **Check a corrected count against what renders, not what the template holds.** #8817
  first wrote "the three docker-proxy services". The third renders only under
  `has_code_server`, which is `false`. Name the members rather than restating a count,
  or the count owes a value row.
- **After a squash merge, run `git fetch --prune` before pushing the next PR's branch.**
  The remote branch is deleted on merge, and a push against the stale tracking ref is
  rejected as `stale info`.
- **An eval case that quotes an edited section must be re-pasted.**
  `evals/tests/test_eval_cases.py` names the case.
- **A stated value may already have a detector.** Grep the tests for the doc's name
  before rewording a number.

## What the citation grammar cannot reach

The batches named three shapes they could not cite. Each is a candidate change to
`scripts/lib/facts/citations.py`, and none is decided.

- **A list entry in YAML.** A YAML atom is a dotted key, so it cannot select the
  `port` of one entry in `cluster/services.yml`. Eight of the owed values have such a
  source.
- **A root-level file.** A path atom needs a slash, so the root `CLAUDE.md` and
  `renovate.json` cannot be cited.
- **An absence.** "Neither Deployment pushes a heartbeat" is true and no atom holds it.
  Only a test can.

## Small leftovers the batches named

- The heading of `k8s/roles/cloudflare-ddns/CLAUDE.md` says the role "keeps two
  Cloudflare A records". It keeps three. The batch left the heading alone, because a
  section's key is its heading text. Change it together with every `# fact:` comment
  and doc that names the key.
- A `DECIDED:` marker in `scripts/dev/prune_worktrees.py` says "the weekly cron
  PRUNES". The sweep is a systemd timer on both k3s hosts. The worktree-cleanup skill
  cites the marker by its text, so the two change together.
- `k8s/roles/game-stats/CLAUDE.md` says `stats_lib.py` is "~250 non-comment lines". The
  batch counted 212.
