# The eight recurring failure classes

This page turns eight failure patterns the review corpus keeps rediscovering into one
record: a class, the incidents that named it, an executable detector where one exists, and
what still needs a human. The classes themselves come from a 2026-08-27 survey of the whole
memory store — every entry in `docs/reference/backlog.md`'s sibling ledgers is an instance of
one of the eight. Read a class before filing a new finding as novel; if it belongs to one, the
class's remedy is the thing to reach for, not another memory entry.

Each detector cell below cites a test node id, in the form `path/to/test_file.py` plus
`::test_name`. `ansible/tests/repo/test_failure_class_detectors.py` parses this table and
asserts every cited node id resolves to a real test in the tree — the same citation shape
`ansible/tests/repo/test_documented_paths_exist.py` already checks for docs generally, applied
here to one page's own claims about coverage. The two guards check different things: that one
proves a node-id citation ANYWHERE in the docs resolves; this page's own meta-test additionally
proves every ROW carries a citation and that the table has not shrunk below its
floor, so a row that quietly lost its detector cannot pass by going empty.

## Coverage table

| # | Class | Incidents cited | Detector | Coverage | What only a human catches |
|---|-------|------------------|----------|----------|----------------------------|
| 1 | Findings get an adversarial pass; fixes get none | 6 of 14 fixes refuted (2026-08-25), 5 of 13 unsafe (2026-08-24) | `ansible/tests/repo/test_fix_skeptic_pass_documented.py::test_the_fix_skeptic_pass_is_still_instructed` | PARTIAL | The guard only proves the fix-skeptic instruction still exists in the skill. Whether a given review session actually dispatched a `skeptic` per remediation, and whether that verdict was honest, is judged by the person reading the report — no test can watch a session run. |
| 2 | The check observes a proxy, not the property | `authelia-302-does-not-prove-the-backend`, `readonly-sa-rollout-restart-reads-as-success`, `image-smoke-false-fails-cli-entrypoint-images`, and six more | `scripts/validate/tests/test_every_validator_has_a_red_proof.py::test_every_validator_has_a_proof_it_can_go_red` | PARTIAL | Scoped to the five modules in `scripts/validate/`. A repo-wide version was measured before writing this page: a fixed rejecting-half vocabulary (`_is_flagged`, `_is_denied`, `_rejects`) missed genuine negative tests written with different words — `.claude/hooks/tests/test_auto_approve_remote_ssh.py::test_destructive_ssh_command_is_left_to_the_prompt` has a real rejecting case a name scan cannot recognize. Deciding whether a new check needs a red-proof pair, and confirming its pair uses whatever vocabulary that file already speaks, stays a human read per file. |
| 3 | Empty is read as clean | `kuma-exports-only-monitors-that-have-beaten`, `cron-path-omits-usr-local-bin`, `longhorn-volume-cr-has-no-status-volumename`, `readonly-sa-forbidden-on-secrets` | `scripts/diagnostics/tests/test_probe_monitors.py::test_kuma_drift_reports_a_declared_monitor_that_is_not_live`; `scripts/dev/tests/test_run_as_cron.py::test_expect_output_fails_a_silent_success`; `ansible/tests/longhorn/test_volume_cr_has_no_volumename.py::test_a_volume_query_reading_volumename_is_flagged` | PARTIAL | Each cited detector covers the one incident it was built for. Recognizing that a NEW query's empty result means "broken" rather than "nothing to report" needs the author to know what a non-empty result would have looked like — that judgment does not generalize into a pattern a script can check. |
| 4 | A guard's selector drifts from the hazard's extent | Nine guards broke on a vacuous glob-based census across six PRs (#838, #846, #852, two in #858, four in the monitor-bridge package move) | `ansible/tests/repo/test_glob_census_non_vacuity.py::test_a_glob_based_census_carries_a_non_vacuity_assertion` | PARTIAL | The detector proves a glob-based census is not silently empty; it cannot prove the census covers the right SET. A derivation that narrows the list it replaces — the incident that named this class — still needs a human to measure the new selector against the one it replaces. Scoped to `ansible/tests/repo/`, `scripts/tests/`, and `scripts/*/tests/`, measured clean before shipping; the other `ansible/tests/` subdirectories were not swept, because a crude version of this same regex over them cannot tell a real gap from a `tmp_path` fixture without a human reading each file — exactly the class-4 trap this page documents. |
| 5 | Sequences treated as transactions | `kubectl-apply-leaves-orphaned-objects`, `kubectl-apply-leaves-stale-secret-keys`, `dual-tagged-task-skipped-by-either-skip-tag`, `longhorn-retain-is-per-job-not-per-volume` | `ansible/tests/deploy/test_dual_tagged_producers.py::test_a_dual_tagged_set_fact_is_flagged`; `ansible/tests/k8s/test_manifest_declares.py::test_the_reader_agrees_with_pyyaml_on_every_rendered_manifest` (feeds `manifest-prune-check.sh`) | PARTIAL | `kubectl apply` leaving a removed Secret key live has no detector at all — CLAUDE.md's own remedy is "patch them out and verify" by hand. Whether a new multi-step sequence needs ordering or atomicity enforcement is a design call made once per sequence; `kubectl apply --prune` is the tempting general fix and is unsafe as usually written, so this class stays mostly human-judged. |
| 6 | Restart is not recreate | Pi containers losing network across a reboot; the docker-proxy stale socket inode; the VPN sidecar's kill-switch rules; Longhorn's `markRemoved` snapshot CR | `ansible/roles/k8s/monitor-bridge/tests/test_check_pi.py::test_dead_port_on_an_up_container_reads_as_detached` | PARTIAL | Only the Pi-reboot instance has a detector, and it catches one shape (an up container publishing no ports). The other three named instances have no detection: recognizing that a NEW restart re-enters the same broken state, rather than clearing it, needs a human who knows what state the restart does not touch. |
| 7 | The execution context is not the shell you developed in | `ansible-target-selects-vars-not-the-machine`, `ssh-lands-in-home-not-repo`, and the doc guards that walked into sibling worktrees | `scripts/dev/tests/test_run_as_cron.py::test_expect_output_fails_a_silent_success`; `ansible/tests/repo/test_no_root_anchored_rglob.py::test_no_tracked_file_rglobs_from_repo_root` | FULL | Both recorded incident shapes have a standing regression guard: a cron's stripped environment (`run_as_cron.sh --expect-output`), and a root-anchored `rglob` walking into `.claude/worktrees/`. A shell-context mismatch this repo has not hit yet — a new tool, a new host — still needs a person to notice the output looked too clean. |
| 8 | Prose asserts facts nothing re-derives | `readonly-sa-forbidden-on-secrets` (docs said "get list watch"), `deploy-time-is-83-percent-waiting`, `autodeploy-counts-must-be-measured-not-computed` | `ansible/tests/repo/test_documented_paths_exist.py::test_every_line_numbered_path_cited_in_the_docs_exists` | FULL | Covers every `file:line` citation across the operator docs. A prose CLAIM with no citable anchor — a stated count, a described behavior with no file reference — is not something this guard can check; the CLAUDE.md rule on "docs quote current values" and its own guard cover the counts, and a factual claim with no citation at all still needs a reader who knows the system to catch it. |

## Reading the FULL/PARTIAL split

FULL means every incident shape the class survey recorded has a standing detector. PARTIAL
means at least one recorded incident is covered and at least one gap remains — either a named
instance with no detector, or a detector that proves less than the class's full hazard. No row
reads NONE: every class had at least one shippable detector once measured against the actual
tree, though class 5's stale-Secret-key instance has none and stays purely a human check within
an otherwise-PARTIAL row.

Three new detectors shipped with this page — `test_glob_census_non_vacuity.py`,
`test_no_root_anchored_rglob.py`, and `test_fix_skeptic_pass_documented.py`. Each was measured
against the live tree before being scoped, per the class-4 lesson this page itself documents: a
detector that finds its subject by pattern is only as good as the census behind it.
