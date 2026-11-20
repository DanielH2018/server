---
id: "0039"
title: scripts/ is installed in place as the one import root, and each tree's own root goes first
status: Accepted
date: 2026-11-19
governs:
  - ".claude/hooks/run-hook.sh#the hook's own scripts/ goes ahead of the venv's .pth"
  - "scripts/lib/longhorn_lib/longhorn_backup_health_logic.py#a shared module keeps its own insert of `hosts/lib`"
---

# ADR-0039: scripts/ is installed in place as the one import root, and each tree's own root goes first

## Status

Accepted. It supersedes the "A virtual uv project, not a package" ruling in
`docs/python-code-organization.md`, and the flat-layout ruling of #2809 once the bootstraps
are gone.

## Context

`scripts/` is a flat set of PEP 420 namespace directories that run uninstalled. A module that
reaches another directory carries its own `sys.path.insert`. At 49e11621c, 391 of the 473
non-test files under `scripts/` changed `sys.path`, and pytest's `pythonpath` listed 13 more
directories under `scripts/`. Every layout incident on record came from two of those
mechanisms disagreeing about `sys.path`, not from a module's short name.

#4435 asked whether to move `scripts/` into an installable `homelab` package under `src/`. The
re-plan of 2026-11-15 rejected the move, because the path is the identifier other things key
on. Two approval lists, 29 prek regexes, 5 workflow `paths:` filters, 12 host units, 1,121
shard-weight keys and several thousand doc citations name `scripts/...`, and about half of
those fail silently when a path goes stale. The move would buy a prefix and a dispatcher.

uv's build backend can install the directory where it stands. With `module-root = "scripts"`
and `namespace = true`, the editable install writes a `.pth` that names `scripts/`, and every
interpreter in the venv then imports `lib.x`, `dev.x` or `renderer.x` with no insert. A run
outside a venv (`uv run --no-project`) gets no `.pth`, so each of its launchers sets
`PYTHONPATH` to its own checkout's `scripts/` (#9271). A GitHub Actions step that runs a
script with the runner's bare `python3` is such a launcher too, and sets `PYTHONPATH: scripts`.
`scripts/lib/stdlib_only.py` reads those steps, and a script reached from one that sets no
root fails `scripts/lib/tests/test_stdlib_only.py::test_every_member_of_the_set_imports_outside_the_venv`.

The install has one effect the re-plan did not foresee, found when slice 1 ran on the real
tree. Several mechanisms run one tree's code under another tree's venv:

- `run-hook.sh`'s `--project` leg runs every session's guards under the primary checkout's
  venv, or the clone's that `RUN_HOOK_PROJECT_DIR` names;
- fan-out runs held hook copies the same way;
- `land_lib.tools.gate_snapshot` runs a snapshot under the primary's venv.

Under each of them the venv's `.pth` adds a second root, the venv's own checkout. A namespace
package merges its portions across roots, so a module the running tree lacks resolves from
the venv's checkout instead of raising `ImportError`. Three tests that pinned a fail-closed
read of a hooks-only copy went red on it.

## Decision

`scripts/` is the project's package, installed in place by `uv_build` as the one import root.
In a venv the editable install supplies it. Outside one, the launcher's `PYTHONPATH` does.
Wherever one tree's code runs under another tree's venv, the launcher puts the running tree's
own `scripts/` first, and a module that tree lacks falls through to the venv's checkout. The
operator ruled on 2026-11-19 that this fall-through is acceptable, over the alternative of one
venv per tree.

## Consequences

- Nothing moves, so no path that names `scripts/` changes.
- `pyproject.toml` pins uv to the range `uv_build` names (`required-version`), so every uv
  that syncs uses its bundled backend and fetches nothing. setup-uv reads the same setting,
  so CI's uv is pinned with it.
- A `sitecustomize` or `usercustomize` module under `scripts/` would now run at every
  interpreter start in the venv, before any landing check. Both names are on the claude-land
  approval list (#9268).
- `land_lib.policy.import_roots` counts `scripts` as a root whether or not a bootstrap
  inserts it.
- A runner that shares another checkout's venv can import the wrong tree's code. A plain
  `uv run` from a snapshot also rewrites the caller's `.pth` to name the snapshot.
  `lib.snapshot_uv` syncs the dependencies without the project and puts the snapshot's
  `scripts/` first (#9269). It orders the roots and does not stop fall-through.
- A test that needs to see a tree with no `scripts/` of its own runs its interpreter with
  `-S`, so no venv root is on the path.
- `lib.host_modules` is the one module under `scripts/` that puts a directory on `sys.path`:
  `hosts/lib` and the gh-quota component's `files/`, whose modules ship to the hosts as flat
  files and import by bare name. A caller imports it before those bare imports.
- `lib.host_modules` also appends each role directory `repo_paths.ROLE_MODULES` lists, such as
  monitor-bridge's `files/` for `longhorn_robustness`. Appending keeps a role's modules from
  shadowing anything else. `deploy_tools/forge.py` imports `lib.host_modules`, so a landing
  counts that role directory among its import roots. The landing gate therefore reads more
  paths as gate code, never fewer.
- A module a host package copies (a `shared:` entry, such as `scripts/lib/longhorn_lib/`) is
  the exception. No `lib` ships beside it, so it cannot import `lib.host_modules`. It keeps its
  own insert of `hosts/lib`, which names a directory that does not exist on the host, and
  imports its own package's siblings relatively (`from . import longhorn_backups`), which
  resolves the same on the host and in the checkout.
- A file a Job stages beside its siblings keeps the staged layout's bare import, as the
  `longhorn_lib` modules do on a host. The soak Job mounts `changed_set.py` beside
  `flux_soak.py`, and `scripts/renderer/tests/conftest.py` binds the bare name to the
  `deploy_tools.changed_set` module object, so the tests see that layout without a second
  copy. A `[[tool.ty.overrides]]` block in `pyproject.toml` exempts both staged files from
  `import-not-found`.
- `python scripts/x.py` without `uv run` raises `ModuleNotFoundError`, because no module under
  `scripts/` inserts its own root. The repo's rule is already to run its Python through
  `uv run`.
- `pytest`'s `pythonpath` lists `scripts` and no subdirectory of it, so a bare sibling import
  fails under pytest as it does in a cron.
- Reverting needs the per-module bootstraps back as well as a revert of the slice 1 PR and
  `uv sync --frozen` on each checkout.

## Governs

- `.claude/hooks/run-hook.sh`, the `--project` leg's `PYTHONPATH`. That leg is the runner
  every session's guards go through, so it is where the running tree's root has to come first.
- `scripts/lib/longhorn_lib/longhorn_backup_health_logic.py`, the shared module's own insert of
  `hosts/lib`. Its four sibling modules carry the same insert for the same reason.
