# Home Assistant Config Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fast, pure-Python prek hook that structurally validates the Home Assistant config (YAML syntax, duplicate keys, broken `!include`s) and syntax-checks all inline + macro Jinja, with no Docker or HA dependency.

**Architecture:** A `scripts/validate_ha_config.py` module assembles the deployed `/config` layout from the role's `templates/` + `files/` (verbatim copies), loads it through an `HAConfigLoader(yaml.SafeLoader)` that recurses `!include`s and rejects duplicate keys, then `Environment().parse()`-checks every `{{ }}`/`{% %}` string and each `custom_templates/*.jinja`. Wired as a `validate-ha-config` prek hook mirroring the existing `validate-compose-templates` pattern.

**Tech Stack:** Python 3.14, PyYAML, Jinja2 (both already in the `dev` group), prek, pytest (via uv).

## Global Constraints

- **`containers/` is read-only** — the validator reads only the role sources under `ansible/roles/containers/home-assistant/`.
- **No Docker, no Home Assistant dependency** — pure Python with PyYAML + Jinja2 only.
- **`HAConfigLoader` MUST subclass `yaml.SafeLoader`** — never the default/unsafe loader. `yaml.load(..., Loader=HAConfigLoader)` is safe because SafeLoader cannot construct arbitrary Python; it is the correct way to register custom tags (`!include`) that `safe_load` cannot.
- **No config file uses `!secret` or Ansible `{{ }}`/`{% %}` templating today** — assembly is a verbatim copy; the validator asserts this and fails loudly if a `templates/*.j2` ever gains Ansible markers.
- **Scope:** structural + Jinja-syntax only. NO HA schema validation (unknown keys / integration options) — that needs Docker `check_config`, deliberately excluded.
- **Mirror the existing pattern:** `scripts/validate_compose_templates.py` + the `validate-compose-templates` hook in `prek.toml`.
- **Commit style:** end messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Stay on `master`.
- **Run tests with:** `uv run pytest scripts/test_validate_ha_config.py -v`.

---

## File Structure

- `scripts/validate_ha_config.py` — **create**: the validator (loader, assembly, structural + Jinja checks, CLI).
- `scripts/test_validate_ha_config.py` — **create**: unit tests (the `scripts` dir is already in `testpaths`).
- `prek.toml` — **modify**: add the `validate-ha-config` hook.

---

### Task 1: Structural validator — assembly, HA-aware loader, duplicate-key + include checks

**Files:**
- Create: `scripts/validate_ha_config.py`
- Test: `scripts/test_validate_ha_config.py`

**Interfaces:**
- Produces: `HAConfigError(Exception)`; `HAConfigLoader(yaml.SafeLoader)` (registers `!include` recursion, `!secret`/`!env_var` placeholders, duplicate-key detection); `assemble_config(role_dir: Path, dest: Path) -> None`; `load_config(dest: Path) -> tuple[list[str], list]` (returns `(structural_error_strings, loaded_trees)` for `configuration.yaml` + `ui-lovelace.yaml`).
- Module constants: `REPO_ROOT`, `ROLE_DIR`, `_TEMPLATE_FILES`, `_STATIC_FILES`, `_ANSIBLE_MARKERS`.

- [ ] **Step 1: Write the failing tests**

Create `scripts/test_validate_ha_config.py`:

```python
"""Tests for scripts/validate_ha_config.py — the lightweight HA config validator."""
import yaml
import pytest

from validate_ha_config import (
    HAConfigError,
    HAConfigLoader,
    ROLE_DIR,
    assemble_config,
    load_config,
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _load(path):
    with open(path) as f:
        return yaml.load(f, Loader=HAConfigLoader)


def test_loader_detects_duplicate_keys(tmp_path):
    p = tmp_path / "dup.yaml"
    p.write_text("recorder:\n  purge_keep_days: 5\nrecorder:\n  purge_keep_days: 7\n")
    with pytest.raises(HAConfigError, match="duplicate key"):
        _load(p)


def test_loader_resolves_include(tmp_path):
    _write(tmp_path / "b.yaml", "- one\n- two\n")
    _write(tmp_path / "a.yaml", "items: !include b.yaml\n")
    assert _load(tmp_path / "a.yaml") == {"items": ["one", "two"]}


def test_loader_missing_include_raises(tmp_path):
    _write(tmp_path / "a.yaml", "x: !include nope.yaml\n")
    with pytest.raises(HAConfigError, match="not found"):
        _load(tmp_path / "a.yaml")


def test_loader_secret_is_placeholder(tmp_path):
    _write(tmp_path / "a.yaml", "token: !secret my_token\n")
    assert _load(tmp_path / "a.yaml") == {"token": "<secret>"}


def test_loader_malformed_yaml_raises(tmp_path):
    _write(tmp_path / "bad.yaml", "foo: [1, 2\n")  # unclosed flow sequence
    with pytest.raises(yaml.YAMLError):
        _load(tmp_path / "bad.yaml")


def test_assemble_rejects_ansible_markers(tmp_path):
    role = tmp_path / "role"
    _write(role / "templates/configuration.yaml.j2", "homeassistant:\n  name: {{ ha_name }}\n")
    _write(role / "templates/customize.yaml.j2", "{}\n")
    _write(role / "templates/ui-lovelace.yaml.j2", "{}\n")
    with pytest.raises(HAConfigError, match="Ansible templating"):
        assemble_config(role, tmp_path / "dest")


def test_real_config_structural_clean(tmp_path):
    assemble_config(ROLE_DIR, tmp_path)
    errors, trees = load_config(tmp_path)
    assert errors == [], errors
    assert len(trees) == 2  # configuration.yaml + ui-lovelace.yaml both loaded
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/test_validate_ha_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'validate_ha_config'`.

- [ ] **Step 3: Create the module (structural half)**

Create `scripts/validate_ha_config.py`:

```python
#!/usr/bin/env python3
"""Lightweight structural validation of the Home Assistant config — no Docker, no HA dependency.

Assembles the deployed /config layout from the home-assistant role's templates + static files,
then validates:
  1. YAML syntax across the whole !include tree.
  2. Duplicate mapping keys (HA rejects them; stock PyYAML silently keeps the last).
  3. Broken !include targets.
  4. Malformed inline Jinja and custom_templates/*.jinja (added in a later step) — a syntax-only
     parse, no rendering.

It does NOT do HA schema validation (unknown keys, integration options) — that needs the real
`hass --script check_config` in a Docker HA image (out of scope; the deploy catches it live).

Run directly (`python3 scripts/validate_ha_config.py`) or via the `validate-ha-config` prek hook.
Exits non-zero if any error is found.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLE_DIR = REPO_ROOT / "ansible/roles/containers/home-assistant"

# templates/*.j2 render verbatim (no Ansible vars) -> copied to <name>.yaml.
_TEMPLATE_FILES = ["configuration.yaml.j2", "customize.yaml.j2", "ui-lovelace.yaml.j2"]
# files/* copied as-is into the config dir root.
_STATIC_FILES = ["automations.yaml", "scenes.yaml", "scripts.yaml", "templates.yaml"]
_ANSIBLE_MARKERS = ("{{", "{%")
# Entry files to structurally load. configuration.yaml pulls in customize/automations/scenes/
# scripts/templates via !include; ui-lovelace.yaml is referenced by filename (not !include), so
# it is loaded explicitly.
_ENTRY_FILES = ["configuration.yaml", "ui-lovelace.yaml"]


class HAConfigError(Exception):
    """A structural problem in the HA config (YAML syntax, duplicate key, broken include)."""


class HAConfigLoader(yaml.SafeLoader):
    """SafeLoader + HA semantics. Subclassing SafeLoader (NOT the unsafe loader) keeps
    `yaml.load(..., Loader=HAConfigLoader)` safe — it cannot construct arbitrary Python — while
    letting us register the `!include`/`!secret` tags that `safe_load` cannot. Each instance
    records its file's directory so `!include` resolves relative to it, matching HA."""

    def __init__(self, stream):
        try:
            self._root = Path(stream.name).resolve().parent
        except AttributeError:
            self._root = Path.cwd()
        super().__init__(stream)

    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in mapping:
                mark = key_node.start_mark
                raise HAConfigError(f"duplicate key {key!r} at {mark.name}:{mark.line + 1}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _construct_include(loader: HAConfigLoader, node: yaml.Node):
    target = (loader._root / loader.construct_scalar(node)).resolve()
    if not target.is_file():
        mark = node.start_mark
        raise HAConfigError(
            f"!include target not found: {target} (at {mark.name}:{mark.line + 1})"
        )
    with target.open() as f:
        return yaml.load(f, Loader=HAConfigLoader)


def _construct_placeholder(loader: HAConfigLoader, node: yaml.Node):
    # We don't validate secret/env values; return a harmless string so the tree loads.
    return f"<{node.tag.lstrip('!')}>"


HAConfigLoader.add_constructor("!include", _construct_include)
HAConfigLoader.add_constructor("!secret", _construct_placeholder)
HAConfigLoader.add_constructor("!env_var", _construct_placeholder)


def assemble_config(role_dir: Path, dest: Path) -> None:
    """Copy the deployed /config layout into dest (verbatim — the templates carry no Ansible vars).

    Raises HAConfigError if a templates/*.j2 contains Ansible templating, which would need a real
    render and violates the repo's copy-not-template rule for HA config files."""
    dest.mkdir(parents=True, exist_ok=True)
    templates = role_dir / "templates"
    files = role_dir / "files"
    for tpl in _TEMPLATE_FILES:
        src = templates / tpl
        text = src.read_text()
        if any(marker in text for marker in _ANSIBLE_MARKERS):
            raise HAConfigError(
                f"{src} contains Ansible templating ({' or '.join(_ANSIBLE_MARKERS)}); the HA "
                "config validator assumes these files are copied verbatim"
            )
        (dest / tpl.removesuffix(".j2")).write_text(text)
    for static in _STATIC_FILES:
        shutil.copy(files / static, dest / static)
    shutil.copytree(files / "custom_templates", dest / "custom_templates")


def load_config(dest: Path) -> tuple[list[str], list]:
    """Structurally load each entry file via HAConfigLoader. Returns (errors, loaded_trees).

    The recursive !include means loading configuration.yaml transitively validates every included
    file's YAML syntax and duplicate keys."""
    errors: list[str] = []
    trees: list = []
    for entry in _ENTRY_FILES:
        path = dest / entry
        try:
            with path.open() as f:
                trees.append(yaml.load(f, Loader=HAConfigLoader))
        except (HAConfigError, yaml.YAMLError) as exc:
            errors.append(f"structural error in {entry}: {exc}")
    return errors, trees
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest scripts/test_validate_ha_config.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/validate_ha_config.py scripts/test_validate_ha_config.py
git commit -m "$(printf 'feat(home-assistant): structural HA config validator\n\nHAConfigLoader (SafeLoader subclass) with !include recursion + duplicate-\nkey detection; assemble_config + load_config. Real config loads clean.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 2: Jinja parse-check + `validate()` orchestration + CLI

**Files:**
- Modify: `scripts/validate_ha_config.py` (append functions; the structural half from Task 1 stays)
- Modify: `scripts/test_validate_ha_config.py` (append tests)

**Interfaces:**
- Consumes: `HAConfigError`, `HAConfigLoader`, `assemble_config`, `load_config`, `ROLE_DIR` (Task 1).
- Produces: `jinja_errors(trees: list, custom_templates_dir: Path) -> list[str]`; `validate(role_dir: Path = ROLE_DIR) -> list[str]`; `main() -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `scripts/test_validate_ha_config.py`:

```python
from validate_ha_config import jinja_errors, validate


def test_jinja_errors_flags_unclosed_inline_block(tmp_path):
    cdir = tmp_path / "ct"
    cdir.mkdir()
    trees = [{"automation": [{"value_template": "{% if x %}no end"}]}]
    errors = jinja_errors(trees, cdir)
    assert errors and "Jinja syntax error" in errors[0]


def test_jinja_errors_flags_bad_macro_file(tmp_path):
    cdir = tmp_path / "ct"
    cdir.mkdir()
    (cdir / "broken.jinja").write_text("{% macro f(x) %}{{ x }}\n")  # missing endmacro
    errors = jinja_errors([], cdir)
    assert any("broken.jinja" in e for e in errors)


def test_jinja_errors_clean_on_valid(tmp_path):
    cdir = tmp_path / "ct"
    cdir.mkdir()
    (cdir / "ok.jinja").write_text("{% macro f(x) %}{{ x | float(0) }}{% endmacro %}\n")
    trees = [{"value_template": "{{ states('sensor.x') | float(0) }}"}]
    assert jinja_errors(trees, cdir) == []


def test_validate_real_config_is_clean():
    # The headline regression guard: the live role config passes structural + Jinja checks.
    assert validate() == []


def test_validate_reports_structural_error(tmp_path):
    role = tmp_path / "role"
    _write(role / "templates/configuration.yaml.j2", "recorder:\n  x: 1\nrecorder:\n  y: 2\n")
    _write(role / "templates/customize.yaml.j2", "{}\n")
    _write(role / "templates/ui-lovelace.yaml.j2", "{}\n")
    for s in ("automations.yaml", "scenes.yaml", "scripts.yaml", "templates.yaml"):
        _write(role / "files" / s, "[]\n")
    (role / "files/custom_templates").mkdir(parents=True)
    errors = validate(role)
    assert any("duplicate key" in e for e in errors)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/test_validate_ha_config.py -v`
Expected: the 5 new tests FAIL with `ImportError: cannot import name 'jinja_errors'` (and `validate`).

- [ ] **Step 3: Append the Jinja check + orchestration**

Add this import near the top of `scripts/validate_ha_config.py` (with the other imports):

```python
from jinja2 import Environment
from jinja2.exceptions import TemplateSyntaxError
```

Append these functions to the end of `scripts/validate_ha_config.py`:

```python
def _iter_template_strings(node):
    """Yield every string in a loaded YAML structure that looks like a Jinja template."""
    if isinstance(node, str):
        if "{{" in node or "{%" in node:
            yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_template_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_template_strings(value)


def jinja_errors(trees: list, custom_templates_dir: Path) -> list[str]:
    """Syntax-check (parse, not render) every inline template string in `trees` and each
    custom_templates/*.jinja file. parse() needs no filters/globals/state, so HA's custom
    filters and `{% from ... import ... %}` don't cause false positives."""
    env = Environment()
    errors: list[str] = []
    for tree in trees:
        for template in _iter_template_strings(tree):
            try:
                env.parse(template)
            except TemplateSyntaxError as exc:
                snippet = template.strip().splitlines()[0][:80]
                errors.append(f"Jinja syntax error: {exc.message} — in: {snippet!r}")
    for jinja_file in sorted(custom_templates_dir.glob("*.jinja")):
        try:
            env.parse(jinja_file.read_text())
        except TemplateSyntaxError as exc:
            errors.append(f"Jinja syntax error in {jinja_file.name}:{exc.lineno}: {exc.message}")
    return errors


def validate(role_dir: Path = ROLE_DIR) -> list[str]:
    """Assemble + structurally load + Jinja-syntax-check the HA config. Returns error strings
    ([] = clean)."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        try:
            assemble_config(role_dir, dest)
        except HAConfigError as exc:
            return [str(exc)]
        errors, trees = load_config(dest)
        # Jinja-check whatever loaded (a structural failure drops that tree but the macro files
        # are checked independently).
        errors += jinja_errors(trees, dest / "custom_templates")
        return errors


def main() -> int:
    errors = validate()
    if errors:
        print("Home Assistant config validation FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("Home Assistant config OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest scripts/test_validate_ha_config.py -v`
Expected: all tests PASS (Task 1's 7 + these 5 = 12).

- [ ] **Step 5: Verify the CLI runs clean against the live config**

Run: `cd /home/ubuntu/server && uv run python scripts/validate_ha_config.py; echo "exit=$?"`
Expected: prints `Home Assistant config OK` and `exit=0`.

- [ ] **Step 6: Commit**

```bash
git add scripts/validate_ha_config.py scripts/test_validate_ha_config.py
git commit -m "$(printf 'feat(home-assistant): Jinja parse-check + validate() CLI\n\nSyntax-check inline templates + custom_templates/*.jinja; validate()\norchestrates assemble+structural+jinja; CLI exits nonzero on errors.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 3: Wire the prek hook

**Files:**
- Modify: `prek.toml` (add the `validate-ha-config` hook after the `validate-compose-templates` hook)

**Interfaces:**
- Consumes: `scripts/validate_ha_config.py` `main()` (Task 2).

- [ ] **Step 1: Add the hook**

In `prek.toml`, immediately after the `validate-compose-templates` hook block (and before the `pytest` hook block), add:

```toml
# Structurally validate the Home Assistant config (pure Python, no Docker): YAML syntax,
# duplicate keys, broken !include targets, and a syntax parse-check of inline + custom_templates
# Jinja. Catches authoring errors before a deploy recreates the container. Schema validation
# (hass --script check_config) is intentionally out of scope.
[[repos.hooks]]
id = "validate-ha-config"
name = "Validate Home Assistant config"
entry = "uv run python scripts/validate_ha_config.py"
language = "system"
pass_filenames = false
files = "^(ansible/roles/containers/home-assistant/(templates|files)/.*|scripts/validate_ha_config\\.py)$"
```

- [ ] **Step 2: Run the hook directly via prek**

Run: `cd /home/ubuntu/server && prek run validate-ha-config --all-files`
Expected: `Validate Home Assistant config` … `Passed`.

- [ ] **Step 3: Confirm the hook triggers on an HA config edit (and still passes)**

Run: `cd /home/ubuntu/server && prek run validate-ha-config --files ansible/roles/containers/home-assistant/files/scripts.yaml`
Expected: the hook runs (not "no files to check") and `Passed`.

- [ ] **Step 4: Full prek + suite sanity**

Run: `cd /home/ubuntu/server && uv run pytest -q && prek run --all-files`
Expected: all tests pass; every prek hook passes (including the new one and the pytest hook picking up `test_validate_ha_config.py`).

- [ ] **Step 5: Commit**

```bash
git add prek.toml
git commit -m "$(printf 'ci(home-assistant): add validate-ha-config prek hook\n\nRuns the structural+Jinja HA config validator on changes under the role\ntemplates/files, locally and in CI.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Self-Review Notes

- **Spec coverage:** YAML syntax + duplicate keys + broken includes (Task 1 loader); Ansible-marker guard (Task 1 assemble); Jinja parse-check of inline + macro files (Task 2); prek hook mirroring validate-compose (Task 3); tests incl. the "real config passes" regression guard (Tasks 1-2). Out-of-scope schema validation is not implemented, as specified.
- **Pre-verified:** a throwaway run of this exact logic against the live config produced 0 structural errors and 0 Jinja false positives across 144 inline template strings + both `.jinja` files, so `test_real_config_structural_clean` and `test_validate_real_config_is_clean` will pass.
- **Type consistency:** `assemble_config(role_dir, dest) -> None`, `load_config(dest) -> (list[str], list)`, `jinja_errors(trees, custom_templates_dir) -> list[str]`, `validate(role_dir) -> list[str]`, `main() -> int` are consistent across the module, tests, and the prek `entry`.
