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

It also does NOT prove an entity still exists. The state-model guardrails below resolve every
reference against `state/external_entities.yml`, which is a **snapshot** refreshed only by an
explicit `ha_state_model.py refresh`. So this catches a **typo** (a name that never existed) and
is structurally blind to a **disappearance** (a name that stopped existing) — both read as
"reference resolves". On 2026-08-16 two Pixel sensors vanished from every device, stayed listed
in the snapshot, and three bedroom features went inert behind a green run here: `states()` on a
missing entity renders `unknown`, which sat inside the automation's own exclusion list, and
`| float(0)` latched a zero the curve treats as a normal night.

Only live HA can see a disappearance: `uv run python scripts/diagnostics/probe.py ha
verify-entities` diffs the snapshot against live state and exits non-zero on anything that went
away. It is a post-deploy gate in the `ha-deploy` skill, deliberately not a step here — this
script has no HA dependency by design, and acquiring one to close the gap would cost more than
the gate does. A green run of this file is not evidence an entity is alive.

Run directly (`python3 scripts/home_assistant/validate_ha_config.py`) or via the `validate-ha-config` prek hook.
Exits non-zero if any error is found.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import yaml
from jinja2 import Environment, nodes
from jinja2.exceptions import TemplateSyntaxError

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import K8S_ROLES

ROLE_DIR = K8S_ROLES / "home-assistant"

# templates/*.j2 render verbatim (no Ansible vars) -> copied to <name>.yaml.
_TEMPLATE_FILES = ["configuration.yaml.j2", "customize.yaml.j2", "ui-lovelace.yaml.j2"]
# files/* copied as-is into the config dir root.
_STATIC_FILES = [
    "scenes.yaml",
    "templates.yaml",
    "rest.yaml",
]
# files/<dir>/ copied as-is as a directory. automations/ is pulled in by
# `automation: !include_dir_merge_list automations/`, scripts/ by
# `script: !include_dir_merge_named scripts/`; custom_templates/ holds the Jinja macros.
_STATIC_DIRS = ["automations", "scripts", "custom_templates"]
_ANSIBLE_MARKERS = ("{{", "{%")
# Entry files to structurally load. configuration.yaml pulls in customize/automations/scenes/
# scripts/templates/rest via !include; ui-lovelace.yaml is referenced by filename (not !include),
# so it is loaded explicitly.
_ENTRY_FILES = ["configuration.yaml", "ui-lovelace.yaml"]


class HAConfigError(Exception):
    """A structural problem in the HA config (YAML syntax, duplicate key, broken include)."""


class HAConfigLoader(yaml.SafeLoader):
    """SafeLoader + HA semantics.

    Subclassing SafeLoader (NOT the unsafe loader) keeps `yaml.load(..., Loader=HAConfigLoader)`
    safe — it cannot construct arbitrary Python — while letting us register the `!include`/`!secret`
    tags that `safe_load` cannot. Each instance records its file's directory so `!include` resolves
    relative to it, matching HA.
    """

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
                raise HAConfigError(
                    f"duplicate key {key!r} at {mark.name}:{mark.line + 1}"
                )
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
        raise HAConfigError(
            f"circular !include: {target} (at {mark.name}:{mark.line + 1})"
        )
    _INCLUDE_STACK.add(target)
    try:
        with target.open() as f:
            return yaml.load(f, Loader=HAConfigLoader)
    finally:
        _INCLUDE_STACK.discard(target)


def _include_dir_files(loader: HAConfigLoader, node: yaml.Node, expected: type):
    """The (path, loaded) pairs behind an `!include_dir_*` tag:

    every *.yaml under the directory (recursive, sorted, dotfiles skipped — HA's `_find_files`
    order).

    Stricter than HA on one point, on purpose: HA silently SKIPS a file whose top level is not the
    expected shape, so an automation file accidentally written as a mapping (or a script file
    written as a list) would ship nothing and surface only as a missing entity at runtime. Here it
    is an error.
    """
    tag = node.tag
    target = (loader._root / loader.construct_scalar(node)).resolve()
    mark = node.start_mark
    if not target.is_dir():
        raise HAConfigError(
            f"{tag} target is not a directory: {target} (at {mark.name}:{mark.line + 1})"
        )
    for path in sorted(target.rglob("*.yaml")):
        if path.name.startswith("."):
            continue
        if path in _INCLUDE_STACK:
            raise HAConfigError(
                f"circular {tag}: {path} (at {mark.name}:{mark.line + 1})"
            )
        _INCLUDE_STACK.add(path)
        try:
            with path.open() as f:
                loaded = yaml.load(f, Loader=HAConfigLoader)
        finally:
            _INCLUDE_STACK.discard(path)
        if not isinstance(loaded, expected):
            raise HAConfigError(
                f"{tag} file {path} must hold a YAML {expected.__name__} at its top level, "
                f"got {type(loaded).__name__} (at {mark.name}:{mark.line + 1})"
            )
        yield path, loaded


def _construct_include_dir_merge_list(loader: HAConfigLoader, node: yaml.Node):
    """`!include_dir_merge_list dir/`: the files' lists concatenated into one list."""
    merged: list = []
    for _, loaded in _include_dir_files(loader, node, list):
        merged.extend(loaded)
    return merged


def _construct_include_dir_merge_named(loader: HAConfigLoader, node: yaml.Node):
    """`!include_dir_merge_named dir/`: the files' mappings merged into one mapping.

    HA lets a later file silently override a key an earlier file defined; here a key that
    appears in two files is an error, so a script cannot be shadowed by a same-named copy.
    """
    merged: dict = {}
    owner: dict = {}
    mark = node.start_mark
    for path, loaded in _include_dir_files(loader, node, dict):
        for key in loaded:
            if key in merged:
                raise HAConfigError(
                    f"{node.tag}: {key!r} is defined in both {owner[key]} and {path} "
                    f"(at {mark.name}:{mark.line + 1})"
                )
            owner[key] = path
        merged.update(loaded)
    return merged


def _construct_placeholder(loader: HAConfigLoader, node: yaml.Node):
    # We don't validate secret/env values; return a harmless string so the tree loads.
    return f"<{node.tag.removeprefix('!')}>"


HAConfigLoader.add_constructor("!include", _construct_include)
HAConfigLoader.add_constructor(
    "!include_dir_merge_list", _construct_include_dir_merge_list
)
HAConfigLoader.add_constructor(
    "!include_dir_merge_named", _construct_include_dir_merge_named
)
HAConfigLoader.add_constructor("!secret", _construct_placeholder)
HAConfigLoader.add_constructor("!env_var", _construct_placeholder)


def assemble_config(role_dir: Path, dest: Path) -> None:
    """Copy the deployed /config layout into dest (verbatim — the templates carry no Ansible vars).

    Raises HAConfigError if a templates/*.j2 contains Ansible templating, which would need a real
    render and violates the repo's copy-not-template rule for HA config files.
    """
    dest.mkdir(parents=True, exist_ok=True)
    # templates/config/, not templates/: the role's templates/ root holds k8s manifests, and
    # validate_k8s_manifests.py parses every .j2 it finds there as YAML.
    templates = role_dir / "templates" / "config"
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
    for static_dir in _STATIC_DIRS:
        shutil.copytree(files / static_dir, dest / static_dir)


def load_config(dest: Path) -> tuple[list[str], list]:
    """Structurally load each entry file via HAConfigLoader. Returns (errors, loaded_trees).

    The recursive !include means loading configuration.yaml transitively validates every included
    file's YAML syntax and duplicate keys.
    """
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
            errors.append(
                f"Jinja syntax error in {jinja_file.name}:{exc.lineno}: {exc.message}"
            )
    return errors


def _macro_names(custom_templates_dir: Path, env: Environment) -> set[str]:
    """Names of every macro defined in custom_templates/*.jinja, via the AST (nodes.Macro) —
    not regex, so comment prose like 'macro argument' is never miscaptured."""
    names: set[str] = set()
    for jinja_file in sorted(custom_templates_dir.glob("*.jinja")):
        try:
            ast = env.parse(jinja_file.read_text())
        except TemplateSyntaxError:
            continue  # syntax errors are reported by jinja_errors
        names |= {m.name for m in ast.find_all(nodes.Macro)}
    return names


def uncoerced_macro_bool_uses(
    template: str, macro_names: set[str], env: Environment | None = None
) -> list[str]:
    """Sorted names of known macros used as a BARE and/or/not operand (no `| bool`) in `template`.

    A `| bool`-wrapped call is a nodes.Filter (not a Call) -> not flagged; a Compare (`== 'x'`) or a
    standalone `{{ macro() }}` is not an and/or/not operand -> not flagged. find_all recurses, so
    nested/chained boolean expressions and operands inside call-args are covered.
    """
    env = env or Environment()
    ast = env.parse(template)

    def bare_macro_call(node):
        if (
            isinstance(node, nodes.Call)
            and isinstance(node.node, nodes.Name)
            and node.node.name in macro_names
        ):
            return node.node.name
        return None

    bad: list[str] = []
    for op in ast.find_all((nodes.And, nodes.Or)):
        for operand in (op.left, op.right):
            name = bare_macro_call(operand)
            if name:
                bad.append(name)
    for neg in ast.find_all(nodes.Not):
        name = bare_macro_call(neg.node)
        if name:
            bad.append(name)
    return sorted(bad)


def macro_bool_coercion_errors(trees: list, custom_templates_dir: Path) -> list[str]:
    """Flag every known-macro call used as a bare and/or/not operand across the inline templates
    (from `trees`) and the custom_templates/*.jinja files. AST-based; deterministic."""
    env = Environment()
    macro_names = _macro_names(custom_templates_dir, env)
    if not macro_names:
        return []
    sources = [t for tree in trees for t in _iter_template_strings(tree)]
    sources += [f.read_text() for f in sorted(custom_templates_dir.glob("*.jinja"))]
    errs: list[str] = []
    for template in sources:
        try:
            for name in uncoerced_macro_bool_uses(template, macro_names, env):
                snippet = template.strip().splitlines()[0][:80]
                errs.append(
                    f"macro {name}() used as a boolean and/or/not operand without "
                    f"`| bool` — a macro renders a STRING (always truthy), so coerce it: "
                    f"in: {snippet!r}"
                )
        except TemplateSyntaxError:
            continue  # reported by jinja_errors
    return errs


# files/<dir> -> the defaults/main.yml variable listing the files the ConfigMap and the init
# container ship from it. HA merges whatever is in the directory, but the pod sees only what
# the list names, so a file in the directory and not the list validates clean and never
# reaches the pod. The check below makes that disagreement an error.
_SHIPPED_DIR_LISTS = {
    "automations": ("home_assistant_automation_files", "*.yaml"),
    "scripts": ("home_assistant_script_files", "*.yaml"),
    "custom_templates": ("home_assistant_template_files", "*.jinja"),
}


def shipped_dir_list_errors(role_dir: Path) -> list[str]:
    """Each list in _SHIPPED_DIR_LISTS must name exactly the matching files under its
    directory, in both directions."""
    defaults = role_dir / "defaults" / "main.yml"
    if not defaults.is_file():
        return []
    values = yaml.safe_load(defaults.read_text()) or {}
    errors = []
    for subdir, (var, pattern) in _SHIPPED_DIR_LISTS.items():
        listed = values.get(var)
        if listed is None:
            continue
        on_disk = sorted(p.name for p in (role_dir / "files" / subdir).glob(pattern))
        for name in sorted(set(on_disk) - set(listed)):
            errors.append(
                f"files/{subdir}/{name} is not in {var} (defaults/main.yml): "
                "the ConfigMap would not carry it"
            )
        for name in sorted(set(listed) - set(on_disk)):
            errors.append(
                f"{var} names {name} but files/{subdir}/ has no such file: "
                "the ConfigMap lookup would fail the deploy"
            )
    return errors


def validate(role_dir: Path = ROLE_DIR) -> list[str]:
    """Assemble + structurally load + Jinja-syntax-check the HA config.

    Returns error strings ([] = clean).
    """
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        try:
            assemble_config(role_dir, dest)
        except HAConfigError as exc:
            return [str(exc)]
        errors, trees = load_config(dest)
        errors += shipped_dir_list_errors(role_dir)
        # Jinja-check whatever loaded (a structural failure drops that tree but the macro files
        # are checked independently).
        errors += jinja_errors(trees, dest / "custom_templates")
        errors += macro_bool_coercion_errors(trees, dest / "custom_templates")
        # State-model guardrails (freshness, entity-resolution, override tripwire, structural).
        try:
            import ha_state_checks

            errors += ha_state_checks.check_errors(role_dir)
        except Exception as exc:  # never let the state-model check mask a config error
            errors.append(f"state-model check crashed: {exc}")
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
