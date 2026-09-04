# Python code organization

How the first-party Python in this repo is laid out, which layout decisions are settled and
why, the conventions a new module follows, and the structural gaps a 2026-09-04 review found
in `land_lib`, `monitor-bridge` and `gitops_deploy`.

This is a reference, not a plan. The findings at the end are ranked and carry `file:line`
evidence as the review found it. Each one was checked against the source by a second reader,
and the four that did not survive are listed as refuted so nobody re-derives them. The rest
were closed in the same PR that added this page; the *Outcome* table at the head of the
findings names the ones that closed partially and why. Line numbers in the findings are the
pre-fix ones.

## The shape of the code

| Measure | Value |
|---|---|
| First-party `.py` files (excluding `ansible/collections/`) | 625 |
| Lines | 146,778 |
| Non-test modules | 179 |
| Test, conftest and `_helper` files | 446 |
| `__init__.py` files | 0 |
| Modules with a `sys.path` bootstrap | 190 |
| Non-test modules over 600 lines | 15 |

Three kinds of Python live here, and they ship by three different mechanisms. The mechanism
decides what a module may import, so it is the first thing to establish about any file.

| Kind | Where | How it reaches a host | May import |
|---|---|---|---|
| Operator scripts | `scripts/<dir>/` | Run from a checkout with `uv run python scripts/<dir>/<name>.py`, by hand or by a cron on `daniel-box` | Anything in the repo, after a bootstrap |
| Role-shipped programs | `ansible/roles/<plane>/<role>/files/` | Ansible copies `files/` to `/opt/<role>/` or into a ConfigMap; the repo is not there | Only what the role's ship list copies beside it |
| Harness hooks | `.claude/hooks/` | Run by Claude Code from the checkout | The repo, after a bootstrap |

The second row is the constraint that shapes everything else. A role-shipped program cannot
`from lib.git import git`, because `scripts/lib/` is not on the host. The two cross-role
shared modules, `host_lib.py` and `bridge/common.py`, are copied beside each consumer by the
consumer's own tasks, and a test proves each ship list matches the tree.

## Decisions that are settled

Each of these was made once and is easy to re-open by accident. The reason and the primary
source are recorded so the next reader can check whether the reason still holds.

### A virtual uv project, not a package

`[tool.uv] package = false` in `pyproject.toml`. uv's own guidance is that a project does not
need a package when it is "writing scripts, building a simple application, using a flat
layout," and needs one to "add commands to the project, distribute the project to others, use
a `src` and `test` layout" ([uv projects config][uv-config]). This repo is the first list.
The consequence is that `[project.scripts]` console entry points are unavailable, and so is
`pip install -e .`. Those are the two things a `src/` layout would buy, so the two decisions
are one decision.

### Flat layout, because the scripts run uninstalled

The Packaging User Guide states the trade plainly: "The src layout requires installation of
the project to be able to run its code, and the flat layout does not"
([src vs flat][src-flat]). Role-shipped programs run on hosts with no checkout and no install
step. pytest's guide "strongly" suggests a `src` layout under the default `prepend` import
mode ([pytest good practices][pytest-good]); that advice is knowingly declined here, and the
unique-basename rule for tests is the price.

### PEP 420 namespace packages, no `__init__.py` anywhere

Every directory under `scripts/` and every role's `files/` resolves as a namespace package
portion. PEP 420 gives the precedence rule: an `__init__.py` anywhere on the path takes
precedence and turns the directory into a regular package ([PEP 420][pep420]). Adding one
changes how pytest names every test module below it, which is why the repo-root `CLAUDE.md`
forbids it. pytest names test modules by basename because `consider_namespace_packages` is
off by default ([pytest pythonpath][pytest-path]), so two test files with the same basename
collide at collection.

### Per-module `sys.path` bootstraps, not `python -m`

A directly run script gets only its own directory as `sys.path[0]` ([sys.path
initialization][syspath]). `python -m scripts.dir.name` would put the current directory
first instead, but that requires every cron and every hook to start from the repo root, and
the role-shipped programs have no repo root at all. So a module that imports across a
directory boundary carries its own insert:

```python
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib.repo_paths import REPO
```

The bootstrap goes on the module that needs it, never on a shared module, because a single
insert in an imported module only works for whoever imports it first.
`scripts/tests/test_script_bootstraps_present.py` resolves the AST of every `scripts/**`
module and evaluates what each insert actually puts on the path, so a missing or wrong
bootstrap fails CI. It does not cover `ansible/roles/**/files/`; those modules resolve their
siblings by bare name from `sys.path[0]`, which is correct by construction on the host.

`pythonpath` in `pyproject.toml` is a pytest setting and nothing else reads it. ty resolves the
same directories through `[tool.ty.environment] extra-paths`, which its docs describe as the
knob for "first-party or third-party modules that are not installed into your project's Python
environment in a conventional way" ([ty configuration][ty-config]);
`ansible/tests/repo/test_ty_config_covers_the_repo.py` keeps the two lists aligned.

### Tests in a sibling `tests/`, never beside the code

A role's `files/` is a ship list. A test inside it would be copied to the host. So tests sit
in `ansible/roles/<plane>/<role>/tests/` and reach `files/` through `pythonpath` or a
bootstrap. `ansible/tests/repo/test_testpaths_covers_every_test_file.py` enforces that every
test directory is in `testpaths`.

### Options considered and not taken

| Option | What it would buy | Why not |
|---|---|---|
| uv workspace, one member per `scripts/<dir>` | Editable inter-member deps replace every bootstrap ([uv workspaces][uv-ws]) | Needs a `pyproject.toml` per directory, and does nothing for role-shipped code |
| PEP 723 inline script metadata on shipped programs | A shipped `files/*.py` declares its own deps on a host with no repo ([PEP 723][pep723]) | The shipped programs are stdlib-only by design, so there is nothing to declare; keep it in mind if one ever needs a dependency |
| `python -m` entry points | Standard-library answer to the bootstrap problem ([`__main__`][main-doc]) | Requires running from the repo root; crons and hooks do not |
| `click` / `typer` | Nicer CLI surface | A third-party dependency on every shipped program, and `argparse` already covers 45 of 60 CLIs |

## Conventions for a new module

These are what the good modules already do. The reference example for each is the file to
copy from.

**Entry point.** `def main(argv: list[str] | None = None) -> int`, parse with `argparse`, and
close with `if __name__ == "__main__": sys.exit(main())`. The function returns an int; it does
not call `sys.exit` itself, so a test can call it. Reference:
`ansible/roles/k8s/uptime-kuma/files/render_status_page.py`. Google's style guide gives the
same shape ([Google Python style, main][google]).

**I/O behind one injectable object.** Every subprocess, HTTP, git and filesystem boundary a
program crosses lives in one frozen dataclass whose fields default to the real implementation.
A test replaces one field and never a `PATH` entry or a module attribute. Reference:
`scripts/deploy_tools/land_lib/tools.py`, with the fakes in
`scripts/deploy_tools/tests/_land_fakes.py`. `monkeypatch` is the fallback for a module that
has no such seam yet, and its count is a measure of how many modules still need one.

**Decision functions are pure.** A function that decides takes plain values and returns plain
values. The transport that fetched them is a different function in a different module.
Reference: `land_lib/merge.py`, which extracts a two-string comparison specifically so the
branch is testable without `gh`, and `monitor-bridge/files/verdicts/` against `checks/`.

**Structure gets a type.** A value that crosses a module boundary is a frozen dataclass or a
`NamedTuple`, not a tuple or a dict of strings. A closed vocabulary (verdicts, causes, tick
states) is a `StrEnum` or `Literal`, so `ty` catches a typo that a runtime frozenset only
catches when the branch runs. Reference: `land_lib/outcome.py` (`Verdict`, `CAUSES`),
`land_lib/tools.py` (`CiVerdict`, the `Classifier` Protocols) and
`monitor-bridge/files/check.py` (`Check`, `CheckResult`). Before the 2026-09-04 review the
tree had 28 dataclasses and no `NamedTuple`, `StrEnum`, `Protocol` or `Literal` at all.

**Exit codes are named once.** A program that has an exit contract defines it in one place
and imports the names at every site that reads a return code.

**Exceptions.** Catch the specific types, and use the PEP 758 unparenthesized form on 3.14:
`except OSError, yaml.YAMLError:`. `except Exception` is for a boundary that must not raise
(a cron's outer loop), and needs a comment saying which boundary. Bare `except:` is a ruff
error.

**Output.** Scripts print. A progress or diagnostic line goes to stderr; the deliverable goes
to stdout. A shipped program running under journald or a container runtime does not stamp
its own lines, because the runtime does. `logging` is not used, and one module adopting it
would be a third convention.

**Datetimes are aware.** ruff's `DTZ` rules gate this; the tree has zero naive `now()` calls.
`time.time()` is fine for an interval.

**Docstrings.** Every module opens with one. A function gets one when it is public, long or
non-obvious. Shape is gated by ruff `D205/D415/D212/D209/D210`; content follows
`~/.claude/rules/python.md`.

**`from __future__ import annotations` is dead on 3.14.** PEP 649 defers annotation
evaluation by default ([What's new in 3.14][py314]). Do not add it to a new module.

**A check ships with a proof it can go red.** Repo-root `CLAUDE.md` owns this rule; it
applies to every validator or guard in this tree.

## What the measurements say

The census below is what the conventions above are measured against. Each row is two
conventions coexisting for one thing; the right-hand column is the one to converge on.

| Thing | Split | Converge on |
|---|---|---|
| Bootstrap spelling | 6 shapes across 190 files; 23 use `os.path` | The aliased `pathlib` form above |
| `main` signature | 40 `main(argv)` / 48 `main()` / 17 unannotated | `main(argv=None) -> int` |
| Exit protocol at the guard | 41 `sys.exit(main())` / 28 `raise SystemExit(main())` / 14 bare `main()` | `sys.exit(main())` |
| CLI parsing | 45 `argparse` / 15 hand-rolled `sys.argv` | `argparse` |
| `from __future__ import annotations` | 331 present / 294 absent | Absent |
| Multi-type `except` | 53 PEP 758 / 28 parenthesized | PEP 758 |
| `@dataclass` frozen | 14 frozen / 13 mutable | Frozen unless a field is written after construction |
| git and gh calls | 12 through `lib.git`/`lib.gh` / ~47 raw argv in production code | The wrapper, where the module can import it |
| Import spelling for `scripts/lib` | `from lib.x import y` / `from lib import x` | Either; do not mix within a module |

None of these is a bug. Each is a rule with no gate, and the census is what a gate would
freeze. The cheapest gates are ruff rules that already exist: `FA102` flags the future
import; `UP` covers the `except` migration. The rest are a repo test over the AST, in the
shape `test_script_bootstraps_present.py` already uses.

## Findings

Ranked by severity. `Confirmed` means a second reader checked the cited lines the day of the
review. Every finding not marked refuted was fixed in the PR that added this page; the table
below records the six that closed partially, with the constraint that stopped them, so the
remainder is not re-derived as a new finding.

### Outcome

| Finding | What closed | What stayed, and why |
|---|---|---|
| 1 `main()` split | `main()` is 58 lines over `assess()`, `plan_tick()` and one `handle_*` per branch; I/O in `deploy_io.py`, composers and the queue's file I/O in `deploy_alerts.py` | `alert_once`, `deliver`, `drain_pending`, `discord`, `check_stale_composes`, `record_staging_tick`, `consult_staging` stay on the entry module: `test_gitops_deploy_patch_boundary.py` requires a patched name to be defined where it is patched, and about 30 tests patch them there |
| 3 two mappers | A test asserts the two mappers agree at the role level over a fixed corpus | `derive` was not rerouted: `role_for` on a `k8s/manifests/` path returns `manifests` where the shared mapper returns a service set, and putting `manifests` in `--tags` makes `deploy.sh` refuse the list |
| 6 config at import | Both programs cannot raise on import; `gitops_deploy` builds a frozen `Config` validated in `main()`, `monitor-bridge` collects `CONFIG_PROBLEMS` and reports them from `main()` with exit 2 | `gitops_deploy` keeps its module constants, derived from `CONFIG`; `STAGING_SUBSET` and two timeouts stay as literal `C.get()` calls because `gen_doc_fragments.py` parses them by text. `HTTP_TIMEOUT` stays in `bridge/common.py` because autofix-bridge imports it from there and does not ship `bridge/config.py` |
| 10 seams | `GateTools` and `NotifyTools`; every subprocess monkeypatch converted | `staging_gate` tests still patch `IDENTITY` and `AUTHORIZED_PUBKEY`, which are filesystem constants rather than process boundaries |
| 16 text assertions | `test_land_merge.py` from 18 `capsys` uses to 8 | The remaining eight are where the printed line is the only discriminator between two paths making one identical `gh` call |
| 25 policy tables | The three R2 billing-class sets moved beside the R2 verdict | `K8S_EXTENDED_RESOURCES` and `PVC_EXCLUDE` are `_env`-read after all; one is rendered into the env Secret and the other is grepped out of `config.py` by a repo test |

Two findings were closed by a comment at the line rather than a change. The `except Outcome:`
in `merge.py` already wrapped a single call; the multi-statement block was the handler. And
`r2_month_start` was already UTC and already pinned by a test.

### High

1. **`gitops_deploy.py` `main()` is one 545-line function.** It holds fetch, CI verdict, hold
   handling, classification, staging consult, deploy dispatch, health gate and alerting, so no
   part of the tick is testable without driving all of it. The `deploy_*` split extracted the
   pure decisions and left every I/O counterpart behind, including three whose sibling module
   already exists by name: `health_ok`/`service_healthy` beside `deploy_health.py`,
   `consult_staging` beside `deploy_staging.py`, `deploy_k8s()` beside `deploy_k8s.py`.
   Evidence: `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py:1353` to `:1898`.
   Confirmed. Next split is by transport, not by more decisions: a `deploy_io.py` holding the
   subprocess, docker and kubectl callers, and a `main()` that only sequences named phases.

2. **The `deploy.sh` exit contract is decoded twice, once as bare integers.**
   `land_lib/deploy.py:142,152,158` compares `rc` to `2`, `75` and `20` inline while
   `staging_gate.py:91` names the same contract as `DEPLOY_SH_NO_VERDICT = frozenset({2, 3, 4,
   75})`. The two can drift with no test between them. Confirmed. One `exit_codes.py` under
   `scripts/deploy_tools/`, imported at both sites. The same module absorbs
   `publish_pr.py:71-78`, which defines two `RC_*` groups that reuse 0 to 3 with different
   meanings.

3. **`land_tags.py` carries two path-to-service mappers, and `derive` uses the local one.**
   Four sites call the deployer's shared `services_from_changed_paths`; `derive` at
   `land_tags.py:436` goes through a local `tag_for`/`role_for` regex instead. A path the two
   classify differently produces a `--tags` list the deployer would not have chosen.
   Confirmed. Route `derive` through the shared mapper, or add a test asserting the two agree
   on a fixed path corpus.

4. **Not one function in the shipped `monitor-bridge` tree has a return annotation.** 0 of 132
   `def` lines under `ansible/roles/k8s/monitor-bridge/files/`, against a fully annotated
   `gitops_deploy`. ty covers both trees through `extra-paths` and has nothing to check on
   half the shipped code. Confirmed by count. Annotate `verdicts/*` and `bridge/*` first;
   those are the pure functions where a wrong type is a silent wrong verdict.

### Medium

5. **The check registry is an untyped 3-tuple returning an untyped pair.**
   `monitor-bridge/files/check.py:83` declares `CHECKS` as `(name, token, fn)` entries and
   each check returns `(ok, msg)`; both are unpacked positionally at `check.py:372,384` and in
   tests. Confirmed. A frozen `Check` dataclass and a `CheckResult`.

6. **Both shipped programs evaluate their whole configuration at import time.**
   `gitops_deploy.py:184` binds `C = cfg()` with about 40 derived constants after it;
   `bridge/config.py` reads roughly 200 env vars at module level. A malformed value raises
   during import, before the heartbeat exists, so the failure is a traceback rather than a
   reportable failed check. monitor-bridge also reads env from three surfaces
   (`bridge/config.py`, `bridge/common.py:48,62`, `check.py:26,318`). One frozen `Config`
   built inside `main()`, validated there.

7. **Three of eight `monitor-bridge` checks keep their thresholds inline with the fetch.**
   `checks/b2.py`, `checks/r2.py` and `checks/storage.py` import nothing from `verdicts/`;
   the other five do. Add the missing verdict modules so the split is uniform.

8. **`Landing` is the inter-phase contract as shared mutable state.** Seven attributes on
   `land_lib/landing.py:31-37` are written by `classify.py` and read by `deploy.py` and
   `health_verdict.py`, and no phase signature says which it touches. `tags` in particular is
   built as a list, joined to a string at `classify.py:77`, and split again at
   `health_verdict.py:25`. Keep `tags` a `list[str]` and join at the subprocess boundary;
   longer term, have each phase return a small frozen result the pipeline threads forward.

9. **`Tools` mixes five pure classifiers into what its docstring calls every process boundary.**
   `land_lib/tools.py:143-147` (`plane_note`, `self_applied`, `remaining_setup_hosts`,
   `derive`, `quiet_paths`) are decision logic, and `_land_fakes.py:172-176` replaces them
   with constant lambdas, so no pipeline test runs real tag derivation. Five of those fields
   are typed `Callable[..., Any]`, which also blinds ty at every call site. Split a
   `Classifier` out of `Tools` and give the remaining callables real signatures.

10. **The top-level `deploy_tools` scripts have no injectable seam.** `test_deploy_detach_notify.py`
    uses `monkeypatch` 31 times, `test_backfill_staging_gate.py` 30, `test_staging_gate.py`
    25, where `land_lib` tests inject a dataclass. `publish_pr.py:101` already has its own
    `Tools`; give `staging_gate.py` and `deploy_detach_notify.py` the same.

11. **`Ledger.cause` is written from seven sites as free-form strings.** Including an f-string
    at `land_lib/deploy.py:161` that makes the value set unbounded, while its sibling
    `verdict` is validated at `outcome.py:42`. The Landings dashboard parses this field. A
    `CAUSES` set beside `VERDICTS`, and a `test_land_ledger.py`, which does not exist:
    `annotation_line` appears in no test.

12. **`await_ci.py` runs git two ways in one module.** `:130` goes through `lib.git`, which
    strips `GIT_*` from the environment; `:143-150` is a raw `subprocess.run(["git",
    "merge-base", ...])` that would follow an inherited `GIT_DIR` to another repository. The
    hazard is documented at `lib/git.py:3-8`. Route the second call through the wrapper.

13. **`read_state` returns `""` for a missing, an empty and an unreadable state file alike.**
    `land_lib/tools.py:120-124` suppresses `OSError`, and `landing.py:97-108` then reports
    `converged` for an unreadable deployer state directory. Distinguish absent from
    unreadable so `tick_state` fails closed.

14. **Deployer state is 15 marker files with 15 constants and bespoke readers.**
    `gitops_deploy.py:91-159`, generic accessors at `:637,645`. No single object describes what
    the host believes. A `DeployerState` class over the same files, no on-disk
    change.

15. **`host_lib.py`'s sibling-copy is re-implemented in seven roles.** Each carries its own
    copy task, `/opt/<x>/host_lib.py` destination and stamp pair (`gitops_deploy/tasks/main.yml:37-40`,
    `renovate_notify`, `renovate_agent`, `k3s/tasks/health-crons.yml`, `fake_remux`, `configarr`,
    `janitorr`). One included task file in `setup/common`, parameterised by destination.

16. **`main` tests assert on printed text about as often as on verdicts.** 30 text assertions
    against 39 verdict or return-code assertions across `test_land_*.py`; `test_land_merge.py`
    uses `capsys` 18 times. A text assertion that stands in for behaviour breaks on a wording
    change. Assert on the fakes' call list or the verdict; keep `capsys` where the line is the
    deliverable.

17. **Neither shipped program has a CLI.** No `argparse` under `monitor-bridge/files`,
    `gitops_deploy/files` or `setup/common/files`, so there is no `--help`, `--once` or
    `--dry-run`; exercising either by hand means setting env vars and running the real
    side-effecting loop. A small `argparse` front end that reuses the same functions the pod
    and the unit call.

18. **The lock-retry loop is written twice with the same shape.** `land_lib/tick.py:25-37` and
    `land_lib/deploy.py:119-133`, same holder-sampling comment. One `retry_while` helper.

### Low

19. Two classes named `Tools` and two named `Outcome` in one directory
    (`publish_pr.py:101,121` vs `land_lib/tools.py:127`, `land_lib/outcome.py:32`).
20. Verdict vocabularies as bare strings: `outcome.py:13`, `landing.py:104-107`,
    `deploy_staging.py:74-81,124-128`. `StrEnum` keeps the on-disk form and gains a type check.
21. Three cross-boundary returns are positional tuples (`tools.py:134,140,146`); `NamedTuple`.
22. `Options` is a mutable dataclass used as immutable config; `conftest.py:92` already treats
    it as frozen via `dataclasses.replace`.
23. `test_land_imports.py:66-73` checks a hardcoded module set with `<=`, so a thirteenth
    `land_lib` module is never checked. Flip to `==`.
24. `_land_fakes.PRIMARY` is a module-level `mkdtemp()` never cleaned, created once per xdist
    worker. Use `tmp_path_factory`.
25. `bridge/config.py:302-458` holds five policy tables that read no env var (the R2 billing
    classes, two exclusion lists). Move them beside the code that applies them.
26. `bridge/common.py:65` stamps its own log lines with a naive local time; the container
    runtime already stamps them. Drop the stamp or make it offset-aware.
27. Step numbering in `land_lib` is owned by two layers, with `/6` hardcoded seven times
    against eight actual steps (`pipeline.py:43,46`, `classify.py:23`, `ci.py:28`,
    `deploy.py:174`, `health_verdict.py:24`, `merge.py:72,140`).
28. `deploy_logic.py` is a pure re-export facade; the entry module's real coupling is invisible
    at `gitops_deploy.py:31`. Keep the facade, import the heavily used modules directly.
29. Two Discord POST implementations (`host_lib.py:62-98`, `bridge/net.py:130-145`) because
    the two programs cannot share a module; only one documents the Cloudflare 1010 user-agent
    workaround. Copy the comment.
30. `EXPORTER_DEPENDENT` and `GATE_DEPENDENTS` in `check.py:204,306` have no every-name-is-a-
    real-check guard, where the five sibling name sets do.

### Refuted

- **"`bridge/common.py` edits do not roll autofix-bridge's pod."**
  `ansible/roles/k8s/autofix-bridge/tasks/main.yml:48-52` hashes every staged module into
  `autofix_bridge_script_checksum`, and `deployment.yaml.j2:29` carries it as the pod-roll
  annotation. The staleness the reviewer suspected is already closed.
- **"Nothing guards a `land_lib` module for a missing bootstrap."**
  `scripts/tests/test_script_bootstraps_present.py` covers every `scripts/**` module by AST
  resolution, including `land_lib`. The direction guard in `test_land_imports.py` is a second
  check, not the only one.
- **"`await_ci.py` duplicates `land_lib/ci.py`."** `ci.py` is a 62-line exit-code-to-verdict
  adapter over the injected `tools.await_ci`; the GitHub polling lives only in `await_ci.py`.
- **"`scripts/lib/release_bin_groups.py` has no importers."** It is imported through the
  `from lib import release_bin_groups` spelling at `scripts/validate/shell_templates.py:41`,
  which a `from lib.` grep misses. That is the import-spelling inconsistency above, not dead
  code.

## Strengths to copy from

- `scripts/deploy_tools/land_lib/tools.py:127-153`: one dataclass holding every process
  boundary with real implementations as defaults.
- `scripts/deploy_tools/tests/test_land_imports.py:28-50,76-82`: an explicit `ALLOWED`
  dependency map plus a reject-half test proving the parser sees both import forms.
- `scripts/deploy_tools/tests/conftest.py:102-127`: an autouse fixture that turns a
  `sys.path` leak into a test failure.
- `scripts/deploy_tools/land_lib/outcome.py:32-50`: exit code and verdict constructed
  together and validated in `__init__`, so "printed without its verdict" is unrepresentable.
- `ansible/tests/services/test_monitor_bridge_modules.py:59-60` and
  `ansible/tests/deploy/test_gitops_deploy_ship_list.py:84-85`: ship lists guarded in both
  directions against the tree.
- `ansible/tests/services/test_monitor_bridge_mount_layout.py:124-135`: a synthesized missing
  module must produce `No module named 'bridge.config'`.
- `pyproject.toml` `addopts`: `-p leakguard` makes "does a test reach the network" a runner
  verdict rather than a review question.

## Sources

[uv-config]: https://docs.astral.sh/uv/concepts/projects/config/
[uv-ws]: https://docs.astral.sh/uv/concepts/projects/workspaces/
[src-flat]: https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/
[pep420]: https://peps.python.org/pep-0420/
[pep723]: https://peps.python.org/pep-0723/
[pytest-good]: https://docs.pytest.org/en/stable/explanation/goodpractices.html
[pytest-path]: https://docs.pytest.org/en/stable/explanation/pythonpath.html
[ty-config]: https://docs.astral.sh/ty/reference/configuration/
[syspath]: https://docs.python.org/3/library/sys_path_init.html
[main-doc]: https://docs.python.org/3/library/__main__.html
[google]: https://google.github.io/styleguide/pyguide.html
[py314]: https://docs.python.org/3.14/whatsnew/3.14.html

- uv: [project configuration][uv-config] and [workspaces][uv-ws]
- Python Packaging User Guide: [src layout vs flat layout][src-flat]
- [PEP 420, implicit namespace packages][pep420] and [PEP 723, inline script metadata][pep723]
- pytest: [good integration practices][pytest-good] and [pythonpath and import modes][pytest-path]
- [ty configuration reference][ty-config]
- Python docs: [`sys.path` initialization][syspath], [`__main__`][main-doc], [What's new in 3.14][py314]
- [Google Python style guide][google], sections 2.2, 3.8 and 3.17
