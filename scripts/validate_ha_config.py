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
from jinja2 import Environment
from jinja2.exceptions import TemplateSyntaxError

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
        # Reject genuine duplicate keys (HA does; stock PyYAML silently keeps the last). Check the
        # EXPLICIT keys only — skip YAML merge keys (`<<`) so a legal merge-override (an explicit
        # key overriding a merged one) is not mis-flagged — then delegate to SafeConstructor, which
        # processes the merge and builds the dict.
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                mark = key_node.start_mark
                raise HAConfigError(f"duplicate key {key!r} at {mark.name}:{mark.line + 1}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


# Tracks the !include files currently being loaded, so a cyclic include (a -> b -> a) raises a
# clear error instead of blowing the stack with an uncaught RecursionError.
_INCLUDE_STACK: set[Path] = set()


def _construct_include(loader: HAConfigLoader, node: yaml.Node):
    target = (loader._root / loader.construct_scalar(node)).resolve()
    mark = node.start_mark
    if not target.is_file():
        raise HAConfigError(
            f"!include target not found: {target} (at {mark.name}:{mark.line + 1})"
        )
    if target in _INCLUDE_STACK:
        raise HAConfigError(f"circular !include: {target} (at {mark.name}:{mark.line + 1})")
    _INCLUDE_STACK.add(target)
    try:
        with target.open() as f:
            return yaml.load(f, Loader=HAConfigLoader)
    finally:
        _INCLUDE_STACK.discard(target)


def _construct_placeholder(loader: HAConfigLoader, node: yaml.Node):
    # We don't validate secret/env values; return a harmless string so the tree loads.
    return f"<{node.tag.removeprefix('!')}>"


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
