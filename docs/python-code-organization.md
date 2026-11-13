# Python code organization

How the first-party Python in this repo is laid out, which layout decisions are settled and
why, and the conventions a new module follows.

This is a reference, not a plan. A 2026-09-04 review of `land_lib`, `monitor-bridge` and
`gitops_deploy` produced ranked structural findings. The PR that added this page fixed every
finding the review did not refute, and git history holds the review.

## The shape of the code

The census below is the 2026-09-04 review's, and the tree has grown since. Recount before
quoting a number.

| Measure | Value |
|---|---|
| First-party `.py` files | 758 |
| Lines | 160,395 |
| Non-test modules | 250 |
| Test, conftest and `_helper` files | 508 |
| `__init__.py` files | 0 |
| Modules with a `sys.path` bootstrap | 232 |
| Non-test modules over 600 lines | 3 |

Three kinds of Python live here, and they ship by three different mechanisms. The mechanism
decides what a module may import, so it is the first thing to establish about any file.

| Kind | Where | How it reaches a host | May import |
|---|---|---|---|
| Operator scripts | `scripts/<dir>/` | Run from a checkout with `uv run python scripts/<dir>/<name>.py`, by hand or by a cron on `daniel-box` | Anything in the repo, after a bootstrap |
| Role-shipped programs | `k8s/roles/<role>/files/` | Ansible copies `files/` to `/opt/<role>/` or into a ConfigMap; the repo is not there | Only what the role's ship list copies beside it |
| Harness hooks | `.claude/hooks/` | Run by Claude Code from the checkout | The repo, after a bootstrap |

The second row is the constraint that shapes everything else. A role-shipped program cannot
`from lib.git import git`, because `scripts/lib/` is not on the host. The two cross-role
shared modules, `host_lib.py` and `bridge/common.py`, are copied beside each consumer by the
consumer's own tasks, and a test proves each ship list matches the tree.

## Decisions that are settled

Each of these was made once and is easy to re-open by accident. The reason and the primary
source are recorded so the next reader can check whether the reason still holds.

### A virtual uv project, not a package

[uv](https://docs.astral.sh/uv/) manages the dev and test tooling from `pyproject.toml` and
`uv.lock`. A virtual project pins its dependencies and the pytest config. `[project]
dependencies` holds only what the renderer imports at runtime, `pyyaml` and `jinja2`. The
`lint` group holds the tools that only prek hooks and hand-run commands call: ruff, ty,
`actionlint-py`, `zizmor` and `mkdocs-material`. The `dev` group holds everything else, which is
the test deps. No group holds `ansible-core` since H7.56.
`[tool.uv] default-groups` names both groups, so `uv run` installs both. `promote.yml` sets
`UV_NO_DEV` and `UV_NO_GROUP=lint`, so a promotion installs those two packages and no tool. A
dependency that a promote job imports therefore goes in `[project] dependencies`. The four
`ci.yml` validator jobs (`pr_gates`, `flux_roundtrip`, `host_packages_build` and
`sops_manifest_type_proof`) set both variables too. Every other job in any workflow that runs
no prek hook sets `UV_NO_GROUP=lint`. actionlint-py ships no wheel and
fetches its binary from a GitHub release, so its install failed every job during a release
outage (#7625). `test_ci_lint_group.py` holds the split. `test_promote_runtime_imports.py` traces every script a `UV_NO_DEV` job runs, in `promote.yml`
and `ci.yml`, and fails on a third-party import outside that list's closure.
`uv run pytest` syncs
the env from `uv.lock` first, and needs [uv installed][uv-install] once. The
`validate-compose-templates` hook shells out to `uv` too, so a full `prek run` needs it as well.

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

Re-examined on 2026-09-28 (#2809) and kept. Packaging `scripts/` as the uv project would retire
the `sys.path` bootstraps and the layout meta-guards, but it would not reach the hosts,
which have no checkout and no install step.

### PEP 420 namespace packages, no `__init__.py` anywhere

Every directory under `scripts/` and every role's `files/` resolves as a namespace package
portion. PEP 420 gives the precedence rule: an `__init__.py` anywhere on the path takes
precedence and turns the directory into a regular package ([PEP 420][pep420]). Adding one
changes how pytest names every test module below it, which is why the repo-root `CLAUDE.md`
forbids it. pytest names test modules by basename because `consider_namespace_packages` is
off by default ([pytest pythonpath][pytest-path]), so two test files with the same basename
collide at collection.

**A new module may not take the name of a namespace-package directory.** A regular module
beats a namespace portion whatever the `sys.path` order, so a `deploy_tools.py` on the path
shadows `scripts/deploy_tools/` and every
`from deploy_tools.deploy_lib.detach_notify import ...` raises `ModuleNotFoundError: 'deploy_tools' is not a package` — for the whole suite, not just for the
role that added the file. That is why the deployer's boundaries object lives in
`deploy_toolbox.py` while the class it holds is `DeployTools`.

The names at risk are the subdirectories of **every** `pythonpath` root in `pyproject.toml`,
not `scripts/` alone. `tests` is a root too, so `deploy`, `k8s`, `longhorn`, `repo`,
`services`, `setup` and `staging` are namespace portions by the same rule, and so are
`bridge`, `checks` and `verdicts` under `k8s/roles/monitor-bridge/files`. Read the
`pythonpath` list, then list what each root holds:

```bash
ls -d scripts/*/ scripts/*/*/ tests/*/ k8s/roles/monitor-bridge/files/*/
```

For an agent PR, the landing policy enforces the rule. `land_lib/policy.py:shape_hits` needs
the operator's approval for a module added beside a directory of the same name, at any depth.
It also needs approval for a module at the top of one `sys.path` root that `land.py` inserts
when it shadows a directory or module at the top of another. It reads those roots from
`land.py`'s own inserts, not from `pythonpath`: the refusal protects what `land.py` imports,
and `pythonpath` applies only under pytest. Across the `pythonpath` roots, every role's `files/`
and every `scripts/<dir>` holding a `tests/`, the
`no-two-import-roots-share-a-module-basename` row of `tests/repo/test_census_rows_python.py`
catches two roots that hold the same module name. No test catches a module that shadows a
directory in another root.

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
module outside a `tests/` directory and evaluates what each insert actually puts on the
path, so a missing or wrong bootstrap fails CI. A module under `tests/` — a `test_*.py` or a
`_*.py` fixture beside it — has pytest as its only invoker, so
`scripts/tests/test_test_module_bootstraps_present.py` refuses the insert there instead
(#2061, #2099). It does not cover `k8s/roles/**/files/`; those modules resolve their
siblings by bare name from `sys.path[0]`, which is correct by construction on the host.

`pythonpath` in `pyproject.toml` is a pytest setting and nothing else reads it. ty resolves the
same directories through `[tool.ty.environment] extra-paths`, which its docs describe as the
knob for "first-party or third-party modules that are not installed into your project's Python
environment in a conventional way" ([ty configuration][ty-config]);
`tests/repo/test_ty_config_covers_the_repo.py` keeps the two lists aligned.

### Tests in a sibling `tests/`, never beside the code

A role's `files/` is a ship list. A test inside it would be copied to the host. So tests sit
in `k8s/roles/<role>/tests/` and reach `files/` through `pythonpath` or a
bootstrap. `tests/repo/test_testpaths_covers_every_test_file.py` enforces that every
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
`scripts/docs/build_docs.py`. Google's style guide gives the
same shape ([Google Python style, main][google]).

**A module family lives in a package behind one entrypoint.** A family is three or more
sibling modules whose filenames share a first or last `_`-word, such as `land_*.py` or
`*_gates.py`, where at least one module is an entrypoint. The entrypoint stays where it is,
and the other modules move into a `<prefix>_lib/` namespace package beside it. A reader
listing the directory then sees one program rather than several. The operator ruled on
2026-10-10 that this navigation gain is reason enough on its own. References:
`scripts/deploy_tools/land.py` with `land_lib/`, `scripts/diagnostics/probe.py` with
`probe_lib/`, and `hosts/components/fake-remux/files/fake_remux.py` with
`fake_remux_lib/`. A forwarding shim at an old path does not count. A shim holds only
imports and a `__main__` guard whose one statement calls the entrypoint of its own family. Shipped code
or an older checkout may still run such a path, so a consolidation can leave a shim there
until the last caller moves. The
`module-families-live-in-a-package` row of `tests/repo/test_census_rows_layout.py`
enforces the rule for a `scripts/` subdirectory and a role's `files/`. Its `allow` names each
family that stays flat, with the reason. Two stay flat by design: the
`scripts/validate/*_templates.py` checks, each its own prek hook, and the deployer's
`gitops_*` modules, which other roles copy by path.

**I/O behind one injectable object.** Every subprocess, HTTP, git and filesystem boundary a
program crosses lives in one frozen dataclass whose fields default to the real implementation.
A test replaces one field and never a `PATH` entry or a module attribute. Reference:
`scripts/deploy_tools/land_lib/tools.py`, with the fakes in
`scripts/deploy_tools/tests/_land_fakes.py`. `monkeypatch` is the fallback for a module that
has no such seam yet, and its count is a measure of how many modules still need one.

The second shape, for a program whose boundary is configuration rather than I/O, is
**config as data**: `monitor-bridge` builds one frozen `Config` from the environment in
`load_config(env)`, constructed once in `cli.py` and passed down. A test builds its own with `load_config({...})` or `dataclasses.replace(cfg,
X=...)` rather than a fakes module, because the fields are values, not callables. The
transport underneath (`bridge.net`) is still patched, which is why
`tests/services/test_bridge_patch_boundary.py` stays and why that role's monkeypatch
entries have not reached zero.

**Decision functions are pure.** A function that decides takes plain values and returns plain
values. The transport that fetched them is a different function in a different module.
Reference: `land_lib/merge.py`, which extracts a two-string comparison specifically so the
branch is testable without `gh`, and `monitor-bridge/files/verdicts/` against `checks/`.

**Structure gets a type.** A value that crosses a module boundary is a frozen dataclass or a
`NamedTuple`, not a tuple or a dict of strings. A closed vocabulary (verdicts, causes, tick
states) is a `StrEnum` or `Literal`, so `ty` catches a typo that a runtime frozenset only
catches when the branch runs. Reference: `land_lib/outcome.py` (`Verdict`, `CAUSES`),
`land_lib/tools.py` (`CiVerdict`, the `Classifier` Protocols) and
`monitor-bridge/files/bridge/types.py` (`Check`, `CheckResult`). Before the 2026-09-04 review the
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
evaluation by default ([What's new in 3.14][py314]). Do not add it to a new module, and
the `no-future-annotations` row of `tests/repo/test_census_rows_python.py` enforces
that the tree agrees —
the rule governed new modules only until 2026-09-05, so half of `scripts/infra_map/` carried
the line and half did not.

**The exception is `k8s/roles/*/files/`**, which the guard exempts. A program there runs
under the HOST's interpreter rather than the repo's uv env — `python3` on daniel-server and
daniel-pi is 3.12.3 — and below 3.14 there is no PEP 649, so annotations evaluate eagerly at
`def`/`class` time and the import is real insurance against a forward reference. The repo suite
cannot see that difference, because it runs on 3.15 where both spellings are lazy.

**A check ships with a proof it can go red.** `.claude/rules/python-layout.md` owns this rule
and its two companions, non-vacuity and a measured transport. They apply to every validator or
guard in this tree.

**Length and `monkeypatch` are ratcheted.** A module may be 450 code lines and a test
module 400. A code line holds a token that is neither a comment nor part of a docstring,
so blank lines, comments and docstrings are free and the cap never argues for deleting
documentation. A test module may patch no first-party module at all — a
patch on the standard library is not counted, because no seam here can remove one. The files
already past either limit are listed one per line in
`tests/repo/module_length_allowlist.txt` and
`tests/repo/monkeypatch_allowlist.txt`, with the number each stands at today, and
`tests/repo/test_module_length_ratchet.py` fails when one grows past its line, when a
file over a cap has no line, when a file that has come back under its cap keeps one, and when
a file has shrunk below its line without the line following it down — an entry records what
the file is today, and the gap between the two would be regrowth headroom nothing reports. The
rules themselves are in `tests/_ratchet.py`. The monkeypatch counter is
`tests/_ratchet_patches.py`, whose docstring is where the counting heuristic's blind
spots are written down.

It also diffs both lists against `origin/master`, so an entry only ever falls or is deleted
and the lists shrink to nothing as the splits land. A path may be added to a list only when
`origin/master` does not track the file — it is new, or renamed — or when the same commit
changes the guard and leaves the file itself alone, since a widened rule finds files that
were always over, and a file that was always over has the content it had on the base.

**The length cap prompts a decision; it does not force a split** (#3661). Split a module
where each piece reads alone. A split whose piece calls methods only its parent defines, or
copies a literal it may not import, adds an interface and hides nothing. Such a module stays
whole, and its length entry ends `# conjoined: <why>`. The ratchet accepts that entry as an
addition. It accepts a rise only in a diff that rewrites the reason to name the new max, so
growth restates the decision and the number it approves. A reason names one line count at
most, the max it approves; the earlier raises are in the commit history, not on the line.
The `# DECIDED:` marker at the top of `module_length_allowlist.txt` is the ruling.
The monkeypatch list takes no reason, because a seam can always remove a patch.

That comparison
skips, saying which reason, when `origin/master` is unreadable: a shallow CI checkout has no
such ref. Locally the ref is only as fresh as your last `git fetch`, so a stale one compares
against older numbers; `git fetch` before relying on it.

## Textual guards under `tests/`

A guard reads rendered output where a render carries the claim, and source text where a render
erases it.

- **Rendered manifests.** A property of the rendered k8s manifests goes in as a row of
  `tests/k8s/test_rendered_properties.py`, not as a regex over a template.
- **Template-source readers are declared.** Every directory under `tests/` is in `SCANNED`
  of `tests/repo/test_guard_tests_read_renders_not_templates.py`. A module that must read
  template text lists itself in that file's `TEMPLATE_SOURCE_READERS` with what a render erases:
  a macro call against its expanded body, byte identity, a Jinja filter, or a variable name.
- **A task file has no render, so the structural read is a parse.** Read `tasks/*.yml` through
  `yaml_fast` or `_helpers.walk_tasks`. Convert a regex over YAML or TOML text to a parse only
  where its claim survives the parse unchanged.
- **The two ratchet lists are a different subject.** `tests/repo/module_length_allowlist.txt`
  and `tests/repo/monkeypatch_allowlist.txt` run on one harness, `tests/_ratchet.py`,
  and neither records a textual read.

## Why the check rules exist

`.claude/rules/python-layout.md` states the rules a new check meets. These are the incidents
behind them.

- **A proof it can go red.** `volume-claim`'s short-circuit shipped behind 16 passing tests
  and a mutation test, then fired for 0 of 25 claims across two full deploys. `image-smoke`'s
  bare-boot rule never caught a real image problem across 11 failures. Both read green
  throughout.
- **A named member.** Nine guards broke by globbing an empty set in six consecutive PRs (#838,
  #846, #852, two in #858, four in the monitor-bridge package move), and the non-vacuity
  assertion alone caught every one.
- **A measured transport.** The Pi-detached arm (PR #482) polled glances'
  `/api/4/containers`, which took 4.43s on an idle Pi and then timed out at the 10s
  `HTTP_TIMEOUT`, where the sibling endpoints answer in 0.03-0.06s. PR #484 reshaped it so the
  cheap signal decides and the expensive one only explains.
- **Retirement.** In the 90 days to 2026-09-28 the repo added 889 test files and deleted 62
  while the service count held near 50. #3023 retired the two tests that pinned the deploy
  and land skills' copies of their exit codes once both tools printed their own.
- **Unique basenames.** Two roles' `app.py` failed six tests only in a full run (#2608). The
  uid validator, then `scripts/validate/grafana_dashboards.py`, hid the filter plugin from
  `pytest scripts` (#5956), because a `scripts/<dir>` holding a `tests/` counts as an import
  root too.
- **Role tests import their own `files/`.** `k8s/conftest.py` added that directory
  in #3746.
- **No `template_filters/tests/`.** Ansible's `PluginLoader._get_paths_with_context` globs two
  levels of subdirectories, so a test there loads at deploy time.

## Strengths to copy from

- `scripts/deploy_tools/land_lib/tools.py`: one dataclass holding every process
  boundary with real implementations as defaults.
- `scripts/deploy_tools/tests/test_land_imports.py`: an explicit `ALLOWED`
  dependency map plus a reject-half test proving the parser sees both import forms.
- `scripts/deploy_tools/tests/conftest.py`: an autouse fixture that turns a
  `sys.path` leak into a test failure.
- `scripts/deploy_tools/land_lib/outcome.py`: exit code and verdict constructed
  together and validated in `__init__`, so "printed without its verdict" is unrepresentable.
- `tests/services/test_monitor_bridge_modules.py`: a ship list guarded in both
  directions against the tree.
- `tests/services/test_monitor_bridge_mount_layout.py`: a synthesized missing
  module must produce `No module named 'bridge.config'`.
- `pyproject.toml` `addopts`: `-p leakguard` makes "does a test reach the network" a runner
  verdict rather than a review question.

## Sources

[uv-config]: https://docs.astral.sh/uv/concepts/projects/config/
[uv-install]: https://docs.astral.sh/uv/getting-started/installation/
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
