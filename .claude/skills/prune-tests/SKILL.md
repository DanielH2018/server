---
name: prune-tests
description: Prune tests that guard no behaviour from a directory of this repo's suite. Use on "prune / trim / thin the tests", "this test guards nothing", "these tests are copies", "a test still asserts something stays removed", or when a suite directory has grown and nobody can say what each test protects. Covers tests already on master; `test-scenario-hygiene` covers the tests a session added.
argument-hint: "[path]"
allowed-tools: Bash, Read, Edit, Grep, Glob
---

# Pruning tests that guard no behaviour

A test earns its place when it depends on intended behaviour: it changes only when that
behaviour changes, and it fails only when that behaviour regresses. This skill finds the tests
under one path that depend on something else, proves each one, and removes what the operator
agrees to.

The rule that permits the removal is *A check retires when its subject does*, in
`.claude/rules/python-layout.md`. This skill adds no rule of its own.

Work one directory per PR. With no path, run step 1's duplicate census over the whole tree,
name the directory that holds the most candidates, and take that one.

## 1. Census the path

Run each command and keep its hits. The step is done when every command has run and each hit
is on a candidate list with the signal that found it.

```bash
uv run python scripts/dev/duplicate_tests.py <path>
git grep -nE "def test_\w*(retired|removed|is_gone|stays_gone|stays_deleted|no_longer|legacy)" -- <path>
git grep -nE "^[A-Z_]+(: [^=]+)? = (frozenset\(\)|set\(\)|\[\]|\(\)|\{\})$" -- <path>
git grep -n "importorskip" -- <path>
git grep -nE "def test_(no|neither|nothing)_\w+" -- <path>
```

- The first lists tests whose bodies are identical
  (`scripts/dev/duplicate_tests.py:duplicate_groups`). `tests/repo/copied_tests_allowlist.txt`
  holds each file's count of them, a new copy fails the ratchet at commit time, and the
  `tighten-ratchet-allowlists` hook lowers a file's line when you prune one.
- The second lists tests named for something that went away.
- The third lists empty allow-lists. A test that loops over one checks nothing.
- The fourth lists skips on a missing import. One that names a declared dependency never skips.
- The fifth lists tests named for an absence. Most are standing invariants over things that
  exist, and those are keeps. About one in twenty names something that was removed.

A hit whose `def` sits inside a string is not a test. The string is source that a test feeds
to a child pytest.

Then read every test module under the path once for what no command finds: an expected value
computed by the code under test, an assertion on a private attribute, and an assertion on
which internal helper ran.

These signals are narrow on purpose. A grep for `not ... .exists()` matches sandbox checks
under `tmp_path`, and the suite uses no mock library, so the usual mock signals find nothing.

## 2. Classify each candidate

Give each candidate exactly one of these, or drop it from the list.

- **The past.** It asserts that removed code stays removed. Bringing the code back would be a
  decision, not a regression. A test that nothing imports or names a module at the path it
  had before a move is this class too.
- **Nothing.** No change to the code under test can make it fail.
- **The implementation.** It fails when the code is restructured and the behaviour is the same.
- **A copy.** Another test already fails for the same regression.

## 3. Move the repo's keeps back

Each rule below overrides step 2. Move the candidate off the list and note which rule applied.

- **A red half.** A check ships with an input it must reject, and that test reads as a copy
  of the accepting one. `hosts/lib/tests/test_reconcile_ufw_arm.py` holds a pair, the second
  named `test_red_...`.
- **A non-vacuity assertion.** An assertion that a census holds named members is the only
  thing that fails when its glob matches nothing. `KNOWN_CONSUMERS` in
  `scripts/diagnostics/tests/probe_cli/test_probe_boundaries.py` is the pattern.
- **A reintroduction that would cause harm.** `tests/services/test_pihole_redundancy.py`
  refuses a path that would restart both DNS replicas at once. A `# DECIDED:` marker beside
  the subject is the same signal.
- **A `legacy:` list.** Reconcile deletes those paths on each host, so a test over one tests
  a mechanism that runs. `hosts/tests/test_legacy_cache_directories.py` is one.
- **A sharded module.** `scripts/renderer/tests/test_renderer_determinism_0.py` and its
  numbered siblings split one workload across workers.
- **An interpreter-grammar parse test.** `hosts/lib/tests/test_host_lib.py` holds the shipped
  scripts to the hosts' own Python.
- **An environment skip.** A skip on a missing binary, on root or on systemd is deliberate.
- **An order assertion that encodes safety.**
  `scripts/deploy_tools/tests/test_land_host_wait_wiring.py` asserts that nothing renders
  before the CI verdict.
- **An identity assertion that prevents a second copy of logic.**
  `k8s/roles/monitor-bridge/tests/test_longhorn_robustness.py` holds one.
- **Copies that each read their own module's subject.** They guard different things. They
  stay, or step 6 rewrites them as one parametrised test with every subject named.

## 4. Prove what is left

A candidate stays on the list only with evidence. The step is done when each one has a line
of output beside it.

- **Nothing, and the implementation:** break the behaviour the test's name claims, in the
  working tree, and run the test. A test that goes red is a keep. Restore the tree with
  `git checkout -- <file>` after each one.
- **A copy:** make the same break and run both tests. The copy goes only when the test that
  stays also goes red.
- **The past:** name the subject and show that the tree no longer holds it, with a `git grep`
  that returns nothing.

## 5. Present and ask

Show three lists with node ids and one reason each: prune, rewrite and kept by step 3. Ask
which to act on. Removing a test is the operator's call.

## 6. Remove

For each test the operator agreed to:

1. Delete it, or rewrite it against the behaviour. A parametrised rewrite of copies asserts
   the set of subjects it covers, so a subject that drops out fails the test.
2. Delete any fixture, helper or constant that nothing else uses.
3. Delete production code that only the removed test called. `uv run pytest
   tests/repo/test_vulture_test_only_names.py` names it.
4. Find every document that cites the node id with `git grep -nF "<file>::<name>" -- '*.md'`.
   Re-point each one, or drop the citation.
   `tests/repo/test_documented_paths_exist.py::test_every_cited_test_exists` fails on a cited
   node id that no longer resolves.

Then run `uv run pytest <path>` and `uv run python scripts/dev/repo_guards.py`.

The PR body names, for each removed test, which of step 2's four it was and its evidence from
step 4. For a copy it names the test that stays.

## Done

- Every test left under the path would fail on a regression, and only on a regression.
- Every removed test has its evidence in the PR body.
- No document cites a removed node id.
- No production code exists only for a removed test.
