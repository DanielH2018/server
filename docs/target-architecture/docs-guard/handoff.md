# Docs guard — handoff of the open work

This page hands the rest of the facts-and-memory plan to another session. It states what
is done as of 2026-11-15, what is open, the order to take it in, and what the sweep
taught. Delete this page in the PR that closes the last open item.

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

1. **The value rows the sweep owed.** Done on 2026-11-15. The rows are in
   `scripts/docs/doc_values_lib/`. One owed value got no row: worktree-cleanup's
   "four-layer ladder". `scripts/lib/worktrees.py:LOCAL_LAYERS` names three layers, and the
   fourth is the forge read, which no constant names.
2. **Mask the generated block in `has_prose`.** Done on 2026-11-15
   ([#8787](https://github.com/DanielH2018/server/issues/8787)). A section that holds only a
   `generated_from` block no longer ranks in the backlog.
3. **The sections the sweep kept back.** Medium effort, in a solo session and not a
   fan-out batch. `.claude/rules/docker.md` has two uncited sections, and
   `.claude/skills/issue-fanout/SKILL.md` has four. The skill describes the batch
   pipeline, so an issue that cites it reads as solo-only.
4. **G11 step 3.** High effort, solo. Rewrite `.claude/rules/facts.md` to the end-state
   model in the G11 plan's design, and replace tiers 2 and 3 of
   `.claude/agents/homelab-docs-freshness-reviewer.md` with a read of the sections that
   `fact_status.py report` ranks as loaded most and cited least. That agent's four
   uncited sections are bound in the same change. `facts.md` loads with every
   instruction file, so a wrong sentence there reaches every session.
5. **The pilot's comparison, due 2026-12-14.** High effort. Count the notice rows that
   the root `CLAUDE.md`'s eight content citations drew in the 30 days after
   [#8707](https://github.com/DanielH2018/server/pull/8707), and grade each unedited
   row as a false sentence or not. G9 measured 0.29 unedited rows per content citation,
   and 6% of them left a false sentence. The sweep's citations can be measured the same
   way from 2026-12-15.
6. **G7 step 5.** High effort, and it needs the operator's ruling first. The step moves
   the replay into `fact_status.py report`, measures a second window, and promotes a
   removed-name class to an error only when fewer than one graded row in five is a
   false alarm. No class was under that bar on 2026-11-14.

Two things are deliberately not on this list. ADR-0031 defers an outbox for memories
written on a replica host until refused writes are counted. Two directive sections cite
nothing by design: "A check that reaches over a network measures its transport first"
in `.claude/rules/python-layout.md`, and "Never print a secret value" in
`.claude/rules/k8s-templates.md`.

## Decisions that stay with the operator

- Whether any removed-name class blocks a commit (G7 step 5).
- What the 2026-12-14 number means for the citations the sweep added.
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
