"""The warning category the role-doc size band is reported through (issue #2557).

`ansible/tests/k8s/test_k8s_roles_have_claude_md.py` raises `RoleDocNearCeiling` for a role
CLAUDE.md that has entered the warning band below its line ceiling, and `pyproject.toml`'s
`filterwarnings` carries an `always::_doc_size.RoleDocNearCeiling` entry beside its blanket
`error` so the notice is SHOWN rather than turned into the failure the band exists to arrive
before.

It is a module of its own for two reasons, both mechanical:

- xdist serializes a warning by its category's module name and the CONTROLLER re-imports that
  module. `pythonpath` puts `ansible/tests` on the path but not its subdirectories, so a
  category defined in `ansible/tests/k8s/<guard>.py` crashes the worker with
  `ModuleNotFoundError` and takes the whole run down with an `INTERNALERROR` (measured
  2026-09-25). The category has to live at this directory's root.
- `_helpers.py`, the obvious home at that root, sat at exactly its 500-line ratchet cap
  (`ansible/tests/repo/test_module_length_ratchet.py`), so adding to it fails CI.
"""


class RoleDocNearCeiling(UserWarning):
    """A role CLAUDE.md within `WARN_LINES..MAX_LINES` of its line ceiling."""
