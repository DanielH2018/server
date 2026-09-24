# Porting deploy.sh to Python behind a thin shim

Issue #2412. **Status: specified 2026-09-24, not started.** This page decides how
`scripts/deploy.sh` becomes a thin exec shim, as `scripts/deploy_tools/land.sh` already is,
over a Python module that imports the deploy helpers instead of spawning them. Implementation
waits on operator approval of this page.

## Why

`scripts/deploy.sh` is 1,293 lines of bash with 19 functions and nine `uv run python`
hand-offs. Each hand-off carries its own timeout, exit-status capture and failure message.
Three of the wrapper's defects were bash-shaped rather than logic-shaped:

- `$?` read after `if ! take_service_locks` was the status of the negation. Both refusal
  arms of the `--detach` path were dead code, and contention exited 76 instead of 75.
- `$SECONDS` rounded a 5ms acquire up to 1s and booked a phantom `lock=1` on the Landings
  board (#1881).
- Stdio inherited from a backgrounded Bash call was non-blocking, and Ansible refused to
  start (#834).

The `DECIDED: the lock order is` note exists only because bash cannot import
`deploy_locks.plan`. `land.sh` went through the same port to `land.py` and its `land_lib`
package.

## Scope

The port changes the implementation and nothing a consumer can observe. The frozen contract
below defines what "nothing" means. Deliberate behaviour changes are out of scope, with one
exception: `LOCK_PLAN_TIMEOUT` goes away (see *Per-helper decisions*).

## The frozen contract

Every item here has a consumer outside `deploy.sh`. The port keeps each one byte-for-byte,
or changes its consumer in the same slice.

### Exit codes

`scripts/deploy_tools/exit_codes.py` defines them once: 0, 2 (tag miss), 3 (broad
`--changed`), 4 (stale tree), 20 (playbook failed), 64 (bad flags), 75 (lock busy),
76 (lock unavailable), 77 (snapshot failed), 78 (no host matched), 79 (lock plan failed).
The Python module imports these names rather than restating the numbers. The consumers are:

- `land_lib/deploy.py`. It retries only on 75, rides out 4, and maps the rest onto verdicts
  in `deploy_outcome`.
- `land_lib/outcome.py`. `_DEPLOY_EXIT_CAUSES` labels each code for the ledger. Exit 1
  labels a failed `cd`, which the shim can still produce.
- `staging_gate.py`. `classify` reads every `DEPLOY_SH_NO_VERDICT` member as NO_VERDICT
  and every other non-zero code as REJECTED. `staging_gate_remote.sh` passes the wrapper's
  exit straight through.
- `.claude/hooks/auto-mode-bridge.py`. `_DEPLOY_EXITS` decodes the codes after a denial.
- `deploy_detach_notify.py`. It gives 78 its own message.

### Parsed output

- `deploy: lock acquired after <N>s` with an optional `(holder was: pid <P> (etimes,
  command): <args>)` on stderr. `land_lib/tools.py:_ACQUIRED` parses it. Printed only when
  the wait was at least one whole second, except on `--detach`, which always prints `0s`.
- `deploy: service lock <name> acquired after <N>s` on stderr, parsed by
  `land_lib/tools.py:_SERVICE_ACQUIRED`. Printed only when the wait was at least one whole
  second.
- `running in background (pid <N>)` on stdout for `--detach`. The pid must be the process
  that holds the snapshot, the service locks and the playbook, because tests wait on its
  exit.
- `--list-services` prints one tag per line on stdout. `deploy_ui.py` splits it.
- The syslog line `event=deploy services=<csv|full> sha=<short> result=ok`, tagged
  `deploy-annotation`, only on exit 0 and never failing the run. Grafana's annotation query
  in the claude-otel role reads it.

### The command line

- The shim's name and path. `block-footguns.py`, `auto-mode-bridge.py`, `land_lib`,
  `deploy_ui.py`, `staging_gate_remote.sh` and every skill invoke `./scripts/deploy.sh`.
- Argument quirks: `--changed` takes an optional positional ref; `--at <sha>` and
  `--at=<sha>`; `-t`, `--tags <csv>` and `--tags=<csv>`; `--list-services` execs and never
  returns; `--check` and `--dry-run` exec `ansible-playbook` from the working tree, unlocked;
  every unrecognised argument passes through to `ansible-playbook` in order.
- The run deploys the checkout that contains the **caller's working directory**
  (`git rev-parse --show-toplevel`), not the checkout the script lives in. `land.sh` does the
  opposite: it `cd`s to its own checkout. Copying the land shim verbatim would change which
  tree a deploy renders.

### Environment variables

`HOMELAB_DEPLOY_TREE_LOCK`, `HOMELAB_DEPLOY_LOCK_DIR`, `HOMELAB_DEPLOY_SNAPSHOT_ROOT`,
`HOMELAB_DEPLOY_TAG_LIST_TIMEOUT` and `HOMELAB_DEPLOY_REAP_MAX_PER_RUN` keep their names and
defaults. Only tests set them, but the `deploy_ui` tests and the `gitops_deploy` conftest set two
of them from outside this directory.

### What other processes observe

`deploy_ui` finds a running deploy two ways, and both break under a naive port:

- `deploy_ui_reads.py:_RUN_RE` matches `deploy.sh` in the `ps` arguments. Once the shim `exec`s
  Python, the process arguments name the Python module instead.
- `deploy_ui_reads.py` reads `/proc/locks` and treats a `->` line as a deploy blocked on a
  lock. `flock -w` blocks inside flock(2), so today a queued deploy shows up there.
  `deploy_locks._take` polls with `LOCK_NB` and sleeps, so a port that reused it would never
  appear as a waiter. The Python module must block in flock(2), bounded by `SIGALRM`, so the
  kernel still lists it.

## Naming

The module is `scripts/deploy_tools/deploy_run.py`. `deploy.py` is taken:
`scripts/docs/tests/test_gen_reference_scripts.py::test_no_two_scripts_share_a_basename`
refuses a second `deploy.py` beside `land_lib/deploy.py`.

## The shim

```bash
#!/usr/bin/env bash
# deploy.sh — the entry point every doc, skill and hook names; it execs deploy_run.py.
here="$(dirname "$(readlink -f "$0")")"
exec uv run --project "$here/.." python "$here/deploy_tools/deploy_run.py" "$@"
```

It does **not** `cd`. The caller's working directory decides which checkout is deployed, and
`--project` makes `uv` resolve the environment from the script's checkout regardless. The
code comes from the script's checkout and the tree from the caller's. In every real
invocation (`land_lib`, `deploy_ui`, the staging runner, an operator typing
`./scripts/deploy.sh`) those are the same checkout.

The subprocess tests stub `uv` on `PATH`, and their stubs end in `*) exit 0`. Through this
shim, a stub would swallow the whole run and report success. `_deploy_sh_fakes.py` therefore
gains a `UV_DEPLOY_RUN_ARM`, built like `UV_DEPLOY_LOCKS_ARM`, that drops the `uv`
arguments up to `python` and runs `deploy_run.py` under `DEPLOY_TEST_PYTHON`. Every stub that runs the wrapper carries that arm first.

## Per-helper decisions

The issue asks for imports. Each helper binds `REPO` to its own file's checkout
(`lib/repo_paths.py:REPO`), so importing changes *which tree* a helper reads unless the call
passes a root explicitly. Each helper is decided separately:

| Helper | Today | Port | Why |
|---|---|---|---|
| `deploy_locks.plan` | `uv run` subprocess, bounded by `LOCK_PLAN_TIMEOUT` | Import | Stdlib-only and pure. The timeout existed to bound a hung interpreter start, which an import cannot have. `LOCK_PLAN_TIMEOUT` and its test are retired. Exit 79 stays, for a plan that raises or names no lock. |
| `deploy_staleness` | A subprocess, cwd is the caller's checkout | Import `main(argv)` with `--repo <repo_root>` | `--repo` already exists and defaults to cwd, so passing it keeps the answer identical. |
| `deploy_tags validate` | A subprocess, `--at <sha>` optional | Import | With `--at` it reads the commit through `git show`. Without it, it reads `HOST_VARS` from the module's checkout, which equals `repo_root` in every real invocation. |
| `deploy_tags changed` | A subprocess | Import | As `validate`. It keeps exit 3 on a broad change. |
| `deploy_tags list` | A subprocess **in the snapshot**, bounded by `TAG_LIST_TIMEOUT` | **Stays a subprocess** in the snapshot | It must read the snapshot's `containers_list` with the snapshot's own parser. An import would read the snapshot's data with the calling checkout's code, which is the version skew #851 fixed for `land_lib`. `--at` makes the skew real: the snapshot can be a newer commit than the caller. |
| `fact_cache_guard --clear` | A subprocess, `\|\| true` | Import, inside `try/except Exception` | Fails open, as the `DECIDED: this preflight fails OPEN` note requires. |
| `deploy_detach_notify` | A subprocess from the detached child, `--cwd <snapshot>` | Import `main(argv)` in the forked child | This preserves the `DECIDED: the two halves of this gate come from different trees` split exactly: notifier code from the caller, probe renders from the snapshot. |
| `ansible-playbook` | `uv run` subprocess | Stays a subprocess | Unchanged, including `UV_PROJECT_ENVIRONMENT=<repo_root>/.venv` and the snapshot as cwd. |

## Locks

`fcntl.flock` and `flock(1)` both call flock(2), so they contend on the same lock. The
GitOps deployer already takes the service locks with `fcntl.flock` in
`deploy_locks.service_locks` while `deploy.sh` takes them with `flock(1)`. That is the
existing proof that a Python wrapper and a bash wrapper exclude each other during rollout.

The Python module takes each lock as follows:

- **Timed wait (`flock -w N -E 75`).** A blocking `fcntl.flock` under `signal.alarm(N)`. The
  handler raises a private timeout exception, which maps to 75. PEP 475 retries an
  interrupted system call unless the handler raises, so the handler must raise. Blocking rather
  than polling keeps the waiter visible in `/proc/locks`.
- **Probe (`flock -n`).** `fcntl.flock(fd, LOCK_EX | LOCK_NB)`. `BlockingIOError` is
  contention (75). Any other `OSError` is 76.
- **Open failure.** `os.open` raising is 76 with the "could not open" message. Today that
  arm is `exec {fd}>` failing.
- **Wait time.** Measured with `time.monotonic()` and floored to whole seconds, which is what
  the `$EPOCHREALTIME` arithmetic does.
- **The holder sample.** Still taken with `fuser` and `ps` before this process opens the
  tree lock, for the reason the bash comment gives: afterwards `fuser` names the caller
  itself.
- **Order.** Tree lock, snapshot, release the tree lock, then `deploy_locks.plan` top to
  bottom. The module never re-takes the tree lock. Both `DECIDED` notes that ADR-0017 governs
  move with the code, and ADR-0017's `governs:` anchors are re-pointed to `deploy_run.py`.

## `--detach`

Today a backgrounded bash child inherits the owner-lock and service-lock descriptors, and the
parent closes its copies. The port uses `os.fork()` after the locks are taken:

- The child calls `os.setsid()`, redirects descriptors 1 and 2 to the log, and runs the playbook, the
  annotation, the notifier and the snapshot cleanup inside `try/finally`.
- The parent closes its copies of the owner-lock and service-lock descriptors, prints
  `running in background (pid <child>)` and the log path, and exits 0 through `os._exit` so
  no `finally` in the parent removes the snapshot.

A flock is released only when every descriptor on its open file description is closed, so
the lock follows the child, as it follows the bash child today.

## Stdio and signals

- `main` clears `O_NONBLOCK` on descriptors 0, 1 and 2 before anything else, as `land.py`'s
  `_prepare_stdio` does (#834).
- The colour decision is made before the pipe: `ANSIBLE_FORCE_COLOR=1` only when stdout is a
  TTY.
- The playbook's stdout is read line by line from a pipe and written both to stdout and to
  an in-memory capture. The recap check strips ANSI escapes and returns the same three
  answers `recap_names_a_host` does. Stderr is inherited, not piped, as today.
- `SIGTERM` and `SIGHUP` get handlers that raise `SystemExit`, so the `finally` that removes
  the snapshot runs. `SIGINT` already raises `KeyboardInterrupt`. The `ansible-playbook`
  child stays in the caller's process group, so a terminal Ctrl-C reaches it directly, as it
  does today.
- `--check`, `--dry-run` and `--list-services` use `os.execvp` and keep their exec semantics.

## Test migration

Each test file that names the wrapper lands in one of three classes:

- **Keep black-box.** The file runs the shim as a subprocess and keeps its assertions. It
  gains the `UV_DEPLOY_RUN_ARM`. Where it stubbed a helper by argv, the stub moves to an
  injected seam or the test runs the real helper.
- **Rewrite as unit.** The file pins behaviour through bash text or a helper's argv. It
  becomes an in-process test of `deploy_run.py` with the helper seams replaced.
- **Re-point.** The file reads source text. It reads `deploy_run.py`, or the constant moves
  into `exit_codes.py` and the test reads the import.

| Test | Class | Note |
|---|---|---|
| `test_deploy_snapshot_failure_names_its_cause.py` | Keep black-box | Real git against a throwaway repo. |
| `test_deploy_snapshot_dir_has_no_comma.py` | Keep black-box | The playbook cwd is still observable through the `uv` stub. |
| `test_deploy_tree_lock_hold_is_bounded.py` | Keep black-box | `deploy_tags list` stays a subprocess, so its stub arm still applies. |
| `test_deploy_exit_codes.py` | Keep black-box | Includes the run with no stubs against the real repo. |
| `test_deploy_service_lock_concurrency.py` | Keep black-box | Real flock. Its fixed-cost bound (two runs under 1s) must be re-measured, because Python start-up plus imports replaces bash start-up. |
| `test_deploy_at_sha.py` | Rewrite as unit | It pins helper argv (`--sha`, `--at`, `--cwd`), and imported helpers have no argv to observe. The `--detach` pid assertion stays black-box. |
| `test_deploy_staleness_precedes_tag_validation.py` | Rewrite as unit | It orders two helper calls by argv log. |
| `test_deploy_staleness_precedes_changed_derivation.py` | Rewrite as unit | As above. |
| `test_wrapper_lock_wait_lines.py` | Rewrite as unit | Its `flock` stubs encode the bash invocation protocol. The unit test keeps the output-line assertions and runs `tools.in_flock_wait` on real output. |
| `ansible/tests/deploy/test_deploy_sh_takes_the_locks_deploy_locks_plans.py` | Rewrite as unit | Its `flock` stub reads `/proc/$$/fd/<n>`. The literal checks (no `server-deploy-`, no `sort -u`, no `LC_ALL`) re-point to `deploy_run.py`. |
| `test_deploy_stamps_are_utc.py` | Re-point | It greps `date` calls. The Python equivalent asserts `datetime.now(UTC)`. |
| `test_deploy_lock_wait_budget.py` | Re-point | `LOCK_WAIT` becomes an importable constant. |
| `ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_timeout_budgets.py` | Re-point | Same constant. |
| `test_exit_codes.py` | Re-point | The module imports the codes, so the literal-in-bash check becomes an identity check. |
| `ansible/tests/deploy/test_deploy_annotations.py` | Re-point | It reads the annotation function's body. |
| `ansible/tests/deploy/test_deploy_runs_from_a_snapshot_under_service_locks.py` | Re-point | It counts `ansible-playbook` call sites and their order. |
| `ansible/tests/deploy/test_k8s_dry_run.py` | Re-point | It reads the `--dry-run` translation. |
| `ansible/tests/repo/test_adr_links.py` | Unchanged | It follows ADR-0017's `governs:` anchors, which move. |
| `ansible/roles/setup/deploy_ui/tests/test_deploy_ui_app.py` | Re-point | It pins the tree-lock default by bash text. |
| `scripts/docs/tests/test_gen_reference_scripts.py` | Unchanged, must stay green | See *Registries*. |

The consumer-side tests are unaffected, because the contract does not change:
`test_land_tools.py`, `test_staging_gate.py`, `test_block_footguns_skip_staleness.py`,
`test_auto_mode_bridge.py` and `test_deploy_skill_names_every_exit_code.py`.

A test that exists only for a bash hazard (the negated `$?`, the `IFS` swap, the comma in
`tag_label`) is retired only after its behaviour is covered by a black-box or unit test. The
comma test stays, because a comma in the snapshot path still breaks ansible.

## Registries and generated pages

- `scripts/lib/script_classify.py` hard-codes `scripts/deploy.sh` as the site that makes
  `deploy_staleness.py`, `deploy_tags.py`, `fact_cache_guard.py` and `deploy_detach_notify.py`
  a `gate`. It finds them with a line regex over bash. Once the shim is a one-line exec, the
  classifier must follow the shim to `deploy_run.py` and count its imports, or those four
  helpers drop out of `gate` and `test_gen_reference_scripts.py` fails.
- `scripts/lib/script_coverage.py` credits `deploy.sh` through tests that name it. The
  credit must move to `deploy_run.py`.
- `docs/reference/decisions.md` is generated from the `DECIDED:` markers and regenerates on
  its own. Hand-written line citations of `deploy.sh` in `gitops-deploy.service.j2` and
  `land_lib/tools.py` are rewritten to cite the marker or symbol.

## Docs that change with the code

`.claude/skills/deploy/SKILL.md`, `docs/deploying.md`, the root `CLAUDE.md` (the
`DEPLOY_SH_NO_VERDICT` paragraph stays true), ADR-0017, `docs/gitops-pipeline.md`,
`ansible/roles/setup/gitops_deploy/CLAUDE.md` (the "exit 79 without the plan" sentence
changes meaning), and `ansible/roles/setup/deploy_ui/CLAUDE.md`. Each changes in the slice
that changes the behaviour it describes.

## Slices

Each slice is one PR that deploys on its own. Each is proven with its tests, one real
`./scripts/deploy.sh --dry-run --tags <svc>`, one real `./scripts/deploy.sh --tags <svc>` of
a low-risk service, and one `land.sh` landing. Rolling back any slice is a revert of its
commit. A broken shim stops every deploy in the fleet, so the revert is also the first
response to any failed deploy after a slice lands.

1. **`deploy_ui` learns the new process shape.** Widen `_RUN_RE` to match `deploy_run.py`
   while still matching `deploy.sh`, then apply the `deploy_ui` role by hand. It is a setup
   role, so the tick does not apply it. This slice lands first so the UI never loses sight of
   a running deploy.
2. **Python front half.** `deploy_run.py` parses the arguments and runs every gate that
   comes before the tree lock: `--at` resolution, `--changed` derivation, the `--detach`
   conflict check, the fact-cache preflight, staleness, tag validation, and the `--check`,
   `--dry-run` and `--list-services` execs. It then execs a residual
   `scripts/deploy_tools/deploy_locked.sh`, which holds today's locked half unchanged and
   reads the resolved arguments from its argv. The shim flips in this slice. The
   `UV_DEPLOY_RUN_ARM` fake and the unit rewrites for the gate-ordering tests land here.
3. **Foreground locked half.** Port the tree lock, snapshot, reaper, `deploy_tags list`
   enumeration, service locks, playbook run, recap check and annotation. `deploy_locked.sh`
   keeps only the `--detach` arm.
4. **`--detach`.** Port the fork, the detached child and the notifier import, then delete
   `deploy_locked.sh`. Re-point ADR-0017's anchors and the remaining text-reading tests, fix
   the registries, and close #2412.

## Concurrent changes to deploy.sh

`deploy.sh` changes often: 14 commits touched it between 2026-09-10 and 2026-09-24. Each slice PR lists every commit in `git log <previous slice>..origin/master --
scripts/deploy.sh` and ports its behaviour into the Python side in the same PR. From slice 2
on, a change to the wrapper's front half goes to `deploy_run.py` only. A change to the
locked half goes to whichever file still holds it.

## Rejected alternatives

- **Port everything in one PR.** About 1,300 lines of bash, a new module of similar size and
  roughly 15 behavioural test files would change at once. A reviewer could not check that
  the contract held, and nothing would be deployable in between.
- **Reuse `deploy_locks.service_locks` for the service locks.** It polls with `LOCK_NB`, so a
  queued deploy disappears from `/proc/locks`. It also reads every `OSError` from flock as
  contention, which merges 75 and 76, and it reports no wait time for the
  `service lock … acquired after` line. Changing it would change the GitOps deployer, a setup
  role that needs a manual apply, for no gain to the deployer.
- **Import `deploy_tags list` as well.** Rejected for the version-skew reason in the helper
  table.
- **Keep `cd` in the shim, as `land.sh` does.** Rejected, because it changes which checkout
  a run from a worktree deploys.
