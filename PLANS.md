# Future Plans

Lightweight idea backlog. Per-feature design rationale lives in
[`docs/superpowers/specs/`](docs/superpowers/specs/); dependency upgrades are tracked by
the Renovate dependency dashboard.

## Backlog

- Try to connect 'daniel-server' Portainer to Pi?

_Recently cleared (2026-06-29): adopted `ruff format` + a `ruff format --check` prek hook
(config kept in `pyproject.toml [tool.ruff]`, not a separate `ruff.toml`). "Organize tests in
scripts/" closed as already-satisfied — every `scripts/` test sits beside the module it tests
and none test role code, so they already follow the repo's co-location convention; a
`scripts/tests/` subdir would only de-colocate them from their bare-name imports. READMEs
reviewed (root / `ansible/` bring-up runbook / `availability_bots`) — current and purpose-distinct,
already cross-linked with no-duplication notes, so no consolidation; refreshed the root README's
stale "Quality gates" list (ruff lint+format, the template validators, secret-rotation sync)._

## Superseded

Completed plans are recorded in git history and in their authoritative homes — each shipped
feature's rationale lives in its role `CLAUDE.md` and its `docs/superpowers/specs/` design doc.
This file keeps only the live backlog scannable; `git log -- PLANS.md ansible/PLANS.md`
recovers the full done-log if needed (the file lived at `ansible/PLANS.md` until 2026-06-29).
