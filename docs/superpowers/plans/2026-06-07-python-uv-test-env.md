# Python dev/test environment via uv — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Manage the repo's Python dev/test environment with uv, giving one source of truth for test deps, a pytest config that runs every repo-owned suite (including `scripts/`) while excluding vendored collections, and a single `uv run pytest` command used locally and by prek/CI.

**Architecture:** A "virtual" uv project (`pyproject.toml` with `[tool.uv] package = false`) declares the `dev` dependency group and `[tool.pytest.ini_options]`. The prek `pytest` and `validate-compose-templates` hooks switch to `language = "system"` and call `uv run …`, eliminating their `additional_dependencies` duplication. CI installs uv via `setup-uv`.

**Tech Stack:** uv, pytest, prek, ansible-core, GitHub Actions.

Spec: `docs/superpowers/specs/2026-06-07-python-uv-test-env-design.md`

---

## File structure

| File | Action | Responsibility |
|------|--------|----------------|
| `pyproject.toml` | Create | Virtual uv project: `dev` dep group + pytest `testpaths`/`pythonpath` |
| `uv.lock` | Create (generated) | Pinned, reproducible dev env |
| `.gitignore` | Modify | Allowlist `pyproject.toml`/`uv.lock`; ignore `__pycache__/`, `.pytest_cache/` |
| `prek.toml` | Modify | `pytest` + `validate-compose-templates` hooks → `uv run` |
| `.github/workflows/ci.yml` | Modify | Add `astral-sh/setup-uv` step |
| `ansible/tests/conftest.py` | Delete | sys.path job moves to `pythonpath` |
| `ansible/roles/containers/monitor-bridge/files/test_check.py` | Modify | unittest → pytest |
| `ansible/tests/test_toposort.py` | Modify | docstring run command |
| `.claude/hooks/test_auto_approve_readonly.py` | Modify | docstring run command |
| `scripts/test_smoke_extract.py` | Modify | add run-command docstring |
| `ansible/roles/containers/monitor-bridge/CLAUDE.md` | Modify | test run command |
| `CLAUDE.md` | Modify | new "Python & Tests" section |

---

## Task 1: Bootstrap uv project, pyproject, lockfile

**Files:**
- Create: `pyproject.toml`
- Create: `uv.lock` (generated)
- Modify: `.gitignore`

- [ ] **Step 1: Install uv** (not present on this box)

Run:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```
Expected: prints a uv version (e.g. `uv 0.x.y`).

- [ ] **Step 2: Create `pyproject.toml`**

```toml
[project]
name = "server-homelab"
version = "0.0.0"
requires-python = ">=3.12"

[tool.uv]
package = false                      # virtual project: manage deps/lock, do not build the repo

[dependency-groups]
dev = [                              # `dev` is uv's default group for `uv run`/`uv sync`
  "pytest>=8",
  "ansible-core>=2.18",             # test_toposort imports ansible.errors
  "pyyaml",                         # validate_compose_templates
  "jinja2>=3",                      # validate_compose_templates
]

[tool.pytest.ini_options]
testpaths = [                        # single source of truth for WHAT runs; excludes vendored
  "ansible/tests",                   # ansible/collections/** third-party tests
  "ansible/roles/containers/monitor-bridge/files",
  ".claude/hooks",
  "scripts",                         # closes the smoke_extract CI gap
]
addopts = "-q"
pythonpath = ["ansible/filter_plugins"]   # lets test_toposort import toposort (replaces conftest.py)
```

- [ ] **Step 3: Generate the lockfile and sync**

Run:
```bash
uv lock
uv sync
```
Expected: creates `uv.lock` and a `.venv/` with pytest, ansible-core, pyyaml, jinja2.

- [ ] **Step 4: Run the full suite via uv**

Run: `uv run pytest`
Expected: PASS. Collects all four repo suites **including `scripts/test_smoke_extract.py`**
(~26 tests), and does **not** collect anything under `ansible/collections/`.

- [ ] **Step 5: Allowlist the new root files and ignore caches in `.gitignore`**

`.gitignore` is allowlist-style (`/*` denies root). Add the two new root files to the
allowlist block (next to `!/renovate.json`):

```diff
 !/renovate.json
+!/pyproject.toml
+!/uv.lock
```

And add cache-ignore hygiene to the "Make sure these are ignored" section (after `*.pyc`):

```diff
 *.pyc
+__pycache__/
+.pytest_cache/
```

(`.venv/` needs no entry — already denied by the root `/*` rule.)

- [ ] **Step 6: Verify the new files are tracked, caches are not**

Run: `git add -A && git status --short`
Expected: `pyproject.toml` and `uv.lock` appear as staged additions; no `.venv/`,
`__pycache__/`, or `.pytest_cache/` entries appear.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock .gitignore
git commit -m "python: add uv-managed dev env (pyproject + lock + pytest config)"
```

---

## Task 2: Remove the conftest.py sys.path shim

**Files:**
- Delete: `ansible/tests/conftest.py`

- [ ] **Step 1: Delete the file**

Run: `git rm ansible/tests/conftest.py`

- [ ] **Step 2: Verify toposort tests still import via `pythonpath`**

Run: `uv run pytest ansible/tests -v`
Expected: PASS — every `test_toposort.py` test passes, proving the `pythonpath` setting in
`pyproject.toml` replaced the conftest sys.path insertion.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "python: drop conftest sys.path shim (replaced by pyproject pythonpath)"
```

---

## Task 3: Convert test_check.py from unittest to pytest

**Files:**
- Modify: `ansible/roles/containers/monitor-bridge/files/test_check.py`

- [ ] **Step 1: Replace the whole file with the pytest version**

Faithful conversion — same assertions, `unittest.TestCase`/`mock.patch.object` →
plain functions + `monkeypatch`, `assertAlmostEqual` → `pytest.approx`. A small `_seq`
helper reproduces `mock`'s `side_effect` list behavior.

```python
#!/usr/bin/env python3
"""Unit tests for the pure logic in check.py (timestamp parsing + backup-age).

Run: uv run pytest ansible/roles/containers/monitor-bridge/files
(or `uv run pytest` for the whole repo suite).

Covers the parts that can be wrong without a live deploy noticing — chiefly the
nanosecond RFC3339 parsing (Kopia emits 9 fractional digits; fromisoformat caps at 6)
and the Kopia /api/v1/sources age/error extraction. The HTTP glue is exercised live
via `check.py --once` at deploy time.
"""
from datetime import datetime, timezone

import pytest

import check


def _seq(*values):
    """Return a callable that yields each value on successive calls (like mock side_effect)."""
    it = iter(values)
    return lambda *a, **k: next(it)


# --- parse_rfc3339 ----------------------------------------------------------

def test_nanosecond_precision_with_z():
    # Real Kopia value: 9 fractional digits + trailing Z
    dt = check.parse_rfc3339("2026-06-06T00:00:00.011699074Z")
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026
    assert dt.microsecond == 11699  # truncated from .011699074


def test_plain_z_no_fraction():
    dt = check.parse_rfc3339("2026-06-06T00:00:00Z")
    assert dt == datetime(2026, 6, 6, tzinfo=timezone.utc)


def test_offset_after_fraction():
    dt = check.parse_rfc3339("2026-06-06T01:00:00.123456789+01:00")
    assert dt.utcoffset().total_seconds() == 3600
    assert dt.microsecond == 123456


# --- backup_age_hours -------------------------------------------------------

NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)


def _sources(last):
    return {
        "sources": [
            {"source": {"path": "/other"}, "lastSnapshot": {"startTime": "2020-01-01T00:00:00Z"}},
            {"source": {"path": "/data/containers"}, "lastSnapshot": last},
        ]
    }


def test_age_from_endtime_and_errors():
    last = {
        "startTime": "2026-06-06T00:00:00.5Z",
        "endTime": "2026-06-06T06:00:00.011699074Z",
        "stats": {"errorCount": 0},
    }
    age, errs = check.backup_age_hours(_sources(last), "/data/containers", now=NOW)
    assert age == pytest.approx(6.0, abs=0.01)  # 06:00 -> 12:00
    assert errs == 0


def test_error_count_surfaced():
    last = {"endTime": "2026-06-06T11:00:00Z", "stats": {"errorCount": 3}}
    age, errs = check.backup_age_hours(_sources(last), "/data/containers", now=NOW)
    assert errs == 3
    assert age == pytest.approx(1.0, abs=0.01)


def test_missing_source_raises():
    with pytest.raises(LookupError):
        check.backup_age_hours({"sources": []}, "/data/containers", now=NOW)


def test_no_snapshot_raises():
    src = {"sources": [{"source": {"path": "/data/containers"}, "lastSnapshot": None}]}
    with pytest.raises(LookupError):
        check.backup_age_hours(src, "/data/containers", now=NOW)


# --- prom_vector ------------------------------------------------------------

def _vector(*pairs):
    """Build a Prometheus instant-query JSON from (labels, value) pairs."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [1700000000, str(val)]}
                for labels, val in pairs
            ],
        },
    }


def test_prom_vector_parses_labels_and_values(monkeypatch):
    payload = _vector(({"name": "sonarr"}, 5), ({"name": "radarr"}, 0))
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: payload)
    out = check.prom_vector("whatever")
    assert out == [({"name": "sonarr"}, 5.0), ({"name": "radarr"}, 0.0)]


def test_prom_vector_empty_result_is_empty_list(monkeypatch):
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: _vector())
    assert check.prom_vector("q") == []


def test_prom_vector_non_success_raises(monkeypatch):
    monkeypatch.setattr(check, "_get_json", lambda *a, **k: {"status": "error"})
    with pytest.raises(RuntimeError):
        check.prom_vector("q")


# --- check_restarts ---------------------------------------------------------

def test_restarts_names_containers_over_threshold(monkeypatch):
    vec = [({"name": "sonarr"}, 5.0), ({"name": "radarr"}, 1.0)]
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: vec)
    ok, msg = check.check_restarts()
    assert not ok
    assert "sonarr" in msg
    assert "radarr" not in msg  # 1 restart is under the default max of 3


def test_restarts_at_threshold_is_ok(monkeypatch):
    # default RESTART_MAX=3; exactly 3 must NOT alert (strictly greater)
    vec = [({"name": "sonarr"}, 3.0)]
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: vec)
    ok, _ = check.check_restarts()
    assert ok


def test_restarts_none_is_ok(monkeypatch):
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    ok, _ = check.check_restarts()
    assert ok


# --- check_oom --------------------------------------------------------------

def test_oom_names_killed_container(monkeypatch):
    vec = [({"name": "n8n"}, 2.0)]
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: vec)
    ok, msg = check.check_oom()
    assert not ok
    assert "n8n" in msg


def test_oom_none_is_ok(monkeypatch):
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: [])
    ok, _ = check.check_oom()
    assert ok


# --- check_targets_down -----------------------------------------------------

def test_targets_names_down_target(monkeypatch):
    vec = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 0.0)]
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: vec)
    ok, msg = check.check_targets_down()
    assert not ok
    assert "cadvisor" in msg
    assert "node" not in msg


def test_targets_all_up_is_ok(monkeypatch):
    vec = [({"job": "node"}, 1.0), ({"job": "cadvisor"}, 1.0)]
    monkeypatch.setattr(check, "prom_vector", lambda *a, **k: vec)
    ok, _ = check.check_targets_down()
    assert ok


# --- check_traefik_5xx ------------------------------------------------------

def test_traefik_high_5xx_with_traffic_alerts(monkeypatch):
    # total 1.0 rps, 0.2 rps of 5xx -> 20% > 5%
    monkeypatch.setattr(check, "prom_scalar", _seq(1.0, 0.2))
    ok, msg = check.check_traefik_5xx()
    assert not ok
    assert "%" in msg


def test_traefik_high_ratio_below_floor_is_ok(monkeypatch):
    # 100% 5xx but only 0.01 rps (< 0.05 floor) -> must NOT alert
    monkeypatch.setattr(check, "prom_scalar", _seq(0.01, 0.01))
    ok, _ = check.check_traefik_5xx()
    assert ok


def test_traefik_low_5xx_is_ok(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", _seq(1.0, 0.01))
    ok, _ = check.check_traefik_5xx()
    assert ok


def test_traefik_no_traffic_metric_is_ok(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", _seq(None, None))
    ok, _ = check.check_traefik_5xx()
    assert ok


# --- check_mem --------------------------------------------------------------

def test_mem_reports_pct_without_oom(monkeypatch):
    # avail 2GB of 10GB -> 80% used, under default 90% -> ok, and no OOM wording
    calls = []
    values = iter([2e9, 10e9])

    def fake(*a, **k):
        calls.append(1)
        return next(values)

    monkeypatch.setattr(check, "prom_scalar", fake)
    ok, msg = check.check_mem()
    assert ok
    assert "OOM" not in msg
    assert len(calls) == 2  # only mem queries, no OOM query


def test_mem_high_alerts(monkeypatch):
    monkeypatch.setattr(check, "prom_scalar", _seq(0.5e9, 10e9))
    ok, msg = check.check_mem()
    assert not ok
    assert "mem" in msg.lower()
```

- [ ] **Step 2: Run the converted suite**

Run: `uv run pytest ansible/roles/containers/monitor-bridge/files -v`
Expected: PASS — same number of test cases as before (24), now as pytest functions.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/monitor-bridge/files/test_check.py
git commit -m "monitor-bridge: convert test_check from unittest to pytest"
```

---

## Task 4: Unify the documented run command across the remaining suites

**Files:**
- Modify: `ansible/tests/test_toposort.py`
- Modify: `.claude/hooks/test_auto_approve_readonly.py`
- Modify: `scripts/test_smoke_extract.py`
- Modify: `ansible/roles/containers/monitor-bridge/CLAUDE.md`

- [ ] **Step 1: `test_toposort.py` docstring**

Replace the `Run:` line in the module docstring.

```diff
-Run: python3 -m pytest ansible/tests/
+Run: uv run pytest ansible/tests
```

- [ ] **Step 2: `test_auto_approve_readonly.py` docstring**

Replace the "Runnable two ways" block:

```diff
-Runnable two ways:
-  * standalone (no deps):   python3 .claude/hooks/test_auto_approve_readonly.py
-  * under pytest:           python3 -m pytest .claude/hooks/test_auto_approve_readonly.py
+Run: uv run pytest .claude/hooks
+(Still importable standalone — it loads the hook by path, no third-party deps.)
```

- [ ] **Step 3: `test_smoke_extract.py` — add a run-command docstring**

The file currently opens with `# scripts/test_smoke_extract.py` then `from smoke_extract import …`.
Replace that first comment line with a module docstring:

```diff
-# scripts/test_smoke_extract.py
+"""Unit tests for smoke_extract.extract_changed_images (image-diff parser).
+
+Run: uv run pytest scripts
+"""
 from smoke_extract import extract_changed_images
```

- [ ] **Step 4: `monitor-bridge/CLAUDE.md` test command**

```diff
-- Unit tests (parsing + every check's decision logic): `cd files && python3 -m unittest test_check`.
-  Also run automatically by the `pytest` prek hook (`prek run pytest --all-files`).
+- Unit tests (parsing + every check's decision logic):
+  `uv run pytest ansible/roles/containers/monitor-bridge/files`.
+  Also run automatically by the `pytest` prek hook (`prek run pytest --all-files`).
```

- [ ] **Step 5: Verify nothing broke**

Run: `uv run pytest`
Expected: PASS (docstring-only changes; same test count as Task 1 Step 4).

- [ ] **Step 6: Commit**

```bash
git add ansible/tests/test_toposort.py .claude/hooks/test_auto_approve_readonly.py scripts/test_smoke_extract.py ansible/roles/containers/monitor-bridge/CLAUDE.md
git commit -m "tests: unify documented run command on 'uv run pytest'"
```

---

## Task 5: Point the prek hooks at uv

**Files:**
- Modify: `prek.toml`

- [ ] **Step 1: Update the comment above the pytest hook**

Replace the existing comment block (currently ends "Runs in prek's isolated env, so it
works regardless of the system Python (ansible here is a pipx install).") with:

```toml
# Run the repo's Python unit tests via uv: the toposort dependency-resolution filters
# (gate every deploy's ordering/scope), the monitor-bridge check logic, the
# auto-approve-readonly Bash classifier (a permission security boundary), and the
# smoke_extract image-diff parser. Deps + test paths come from pyproject.toml (the `dev`
# group + [tool.pytest.ini_options] testpaths); `uv run` auto-syncs from uv.lock, so it
# works regardless of the system Python. Requires uv on PATH (CI installs astral-sh/setup-uv).
```

- [ ] **Step 2: Update the pytest hook**

```diff
 [[repos.hooks]]
 id = "pytest"
-name = "Run Python unit tests (toposort + monitor-bridge + readonly-hook)"
-entry = "python3 -m pytest"
-language = "python"
-additional_dependencies = ["pytest", "ansible-core>=2.18", "pyyaml"]
+name = "Run Python unit tests (toposort + monitor-bridge + readonly-hook + smoke)"
+entry = "uv run python -m pytest"
+language = "system"
 pass_filenames = false
-args = ["ansible/tests", "ansible/roles/containers/monitor-bridge/files", ".claude/hooks"]
-files = "^(ansible/filter_plugins/.*\\.py|ansible/tests/.*\\.py|ansible/roles/containers/monitor-bridge/files/.*\\.py|\\.claude/hooks/.*\\.py)$"
+files = "^(ansible/filter_plugins/.*\\.py|ansible/tests/.*\\.py|ansible/roles/containers/monitor-bridge/files/.*\\.py|\\.claude/hooks/.*\\.py|scripts/.*\\.py|pyproject\\.toml)$"
```

- [ ] **Step 3: Update the validate-compose-templates hook**

```diff
 id = "validate-compose-templates"
 name = "Validate rendered docker-compose templates"
-entry = "python3 scripts/validate_compose_templates.py"
-language = "python"
-additional_dependencies = ["jinja2>=3", "pyyaml"]
+entry = "uv run python scripts/validate_compose_templates.py"
+language = "system"
 pass_filenames = false
```

(Leave that hook's `files` line unchanged.)

- [ ] **Step 4: Run both hooks via prek (using uv)**

Run:
```bash
prek run pytest --all-files
prek run validate-compose-templates --all-files
```
Expected: both report `Passed`. (uv must be on PATH — it is, from Task 1 Step 1.)

- [ ] **Step 5: Commit**

```bash
git add prek.toml
git commit -m "prek: run pytest + compose validation via uv (single dep source)"
```

---

## Task 6: Install uv in CI

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add the setup-uv step**

Insert between `setup-python` and `Install prek`:

```diff
       - uses: actions/setup-python@v5
         with:
           python-version: "3.12"

+      - name: Install uv
+        uses: astral-sh/setup-uv@v5
+
       - name: Install prek
         run: pip install prek
```

- [ ] **Step 2: Lint the workflow YAML**

Run: `uv run python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: install uv so prek hooks can run tests via uv"
```

---

## Task 7: Document the Python & Tests setup in CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Insert a "Python & Tests" section after "## Pre-commit Hooks"**

Add before the `## Variables` section:

```markdown
## Python & Tests
Dev/test tooling is managed by [uv](https://docs.astral.sh/uv/) (`pyproject.toml` + `uv.lock`).
The repo isn't a Python package — `[tool.uv] package = false` makes it a "virtual" project that
only pins the test deps (the `dev` dependency group) and the pytest config.

```bash
# One-time: install uv — https://docs.astral.sh/uv/getting-started/installation/
uv run pytest                 # all repo unit tests (auto-syncs the env from uv.lock first)
uv run pytest scripts         # just one suite
```

- **What runs is defined once** in `pyproject.toml` `[tool.pytest.ini_options]` `testpaths` —
  consumed by both `uv run pytest` and the prek `pytest` hook. It deliberately excludes the
  vendored `ansible/collections/**` third-party tests.
- **Deps live once** in the `dev` dependency group; the prek `pytest` and
  `validate-compose-templates` hooks call `uv run`, so there's no duplicated dependency list.
  **uv must be on `PATH` for `prek run`** (CI installs it via `astral-sh/setup-uv`).
- **Suites:** `ansible/tests/` (toposort deploy-ordering filters),
  `ansible/roles/containers/monitor-bridge/files/` (Kopia/Prometheus check logic),
  `.claude/hooks/` (read-only Bash classifier), `scripts/` (image-diff parser).
- **Test-placement gotcha:** pytest tests must NOT live under `ansible/filter_plugins/` —
  Ansible's plugin loader imports every `.py` there at deploy time and would choke on the
  `pytest` import. `test_toposort.py` lives in `ansible/tests/` and imports its target via the
  `pythonpath` setting in `pyproject.toml`.

CI (`.github/workflows/ci.yml`) runs `prek run --all-files` on every PR and on push to master:
these tests plus lint, template validation, and secret scanning.
```

- [ ] **Step 2: Sanity-check the doc renders and tests still pass**

Run: `uv run pytest && prek run --all-files`
Expected: pytest PASS; all prek hooks `Passed`.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document uv-managed Python test setup in CLAUDE.md"
```

---

## Self-review notes

- **Spec coverage:** pyproject/dep-group/pytest-config (Task 1) ✓; uv.lock (Task 1) ✓;
  `.gitignore` allowlist (Task 1) ✓; prek hooks → uv (Task 5) ✓; CI setup-uv (Task 6) ✓;
  conftest deletion (Task 2) ✓; test_check conversion (Task 3) ✓; docstring unification
  (Task 4) ✓; CLAUDE.md docs — the original ask — (Task 7) ✓.
- **No placeholders:** every code/edit step shows literal content.
- **Type/name consistency:** `dev` group name, `testpaths`, `pythonpath`, and the
  `uv run python -m pytest` entry are identical across Tasks 1, 5, 6, 7.
- **Extra doc reference found:** `monitor-bridge/CLAUDE.md`'s `python3 -m unittest` line is
  folded into Task 4 Step 4.
```
