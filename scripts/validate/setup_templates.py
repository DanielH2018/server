#!/usr/bin/env python3
"""Render every host component template and fail on a variable nothing defines.

The other render guards cover other files: `compose_templates.py` reads the Pi's `pi-compose`
services, `k8s_manifests.py` reads `roles/k8s`, `shell_templates.py` covers `*.sh.j2` anywhere
and `unit_templates.py` covers `*.service.j2` / `*.timer.j2`. Nothing else renders a component's
`*.yaml.j2`, `*.env.j2`, `*.conf.j2` or Corefile. A template with an undefined variable, a Jinja
syntax error, or a rename its consumer missed would otherwise first fail in a package build.

WHAT COUNTS AS UNDEFINED. Rendering with a bare `StrictUndefined` would fail on every SOPS
secret and every key a host component supplies, so this guard renders with a tracking
Undefined and then judges each name it collected. A name is allowed to be undefined when it is:

  - supplied by the host renderer (`renderer_lib/host.py`) — `hostvars`, `inventory_hostname`
    and `playbook_dir` (`HOST_RENDER_SUPPLIED`);
  - a SOPS secret, by name, from the plaintext registry `ansible/secret_rotation.yml`;
  - defined anywhere in the inventory — `group_vars/all.yml` or any host's `host_vars` file;
  - declared in the component's own `defaults/main.yml`. `lib.render_context` leaves out a
    default whose value will not expand (it names a filter the light environment lacks), so the
    key reads as undefined in the render while the component still defines it;
  - supplied by the component, or by a component that borrows it with `component:` — a key of
    its `defaults:`, or `<key>_value` for a secret key one of its `outputs:` declares.

Everything else is a failure naming the template and the variable. That is the shape a rename
takes: the producer moves and the consumer keeps the old name, which then matches none of the
sources above.

The YAML templates (`*.yaml.j2` / `*.yml.j2`) are additionally parsed, the same structural check
the other three guards make. The rest are rendered only — a `.env`, a Corefile and libvirt XML
have no shared parser, and their shell/unit siblings already have validators of their own.

Run directly or via the ``validate-setup-templates`` prek hook. Exits non-zero on any render
failure, unresolved variable, or invalid YAML.
"""

import sys
from collections.abc import Sequence
from collections import defaultdict
from pathlib import Path

import yaml
from jinja2 import ChainableUndefined

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from validate.validate_lib.ok_lines import ok_printer
from lib.cli_help import answer_help
from lib import yaml_fast
from lib.ansible_jinja_env import template_env
from lib.render_context import render_context
from lib.render_guard import (
    ALL_VARS,
    ANSIBLE,
    dump_numbered,
    host_files,
    load_yaml,
    render_or_error,
)
from lib.repo_paths import REPO

COMPONENTS = REPO / "hosts" / "components"
SECRET_REGISTRY = ANSIBLE / "secret_rotation.yml"

# The names `renderer_lib/host.py` supplies a component template, which no repo file defines.
HOST_RENDER_SUPPLIED = frozenset({"hostvars", "inventory_hostname", "playbook_dir"})

# The host the renderer sets `inventory_hostname` to, given a plausible value here.
HOST_RENDER_CONTEXT = {"inventory_hostname": "daniel-box"}


def _tracking_undefined(seen: dict[str, set[str]], template: str):
    """A Jinja Undefined that records the name it stood in for instead of raising.

    Renders as the literal ``STUB`` (the same fill the other guards use) so a single missing
    value does not abort the render and hide every later problem in the same file. `main()`
    judges the collected names afterwards.
    """

    class Tracking(ChainableUndefined):
        _FILL = "STUB"

        def _record(self):
            seen[template].add(self._undefined_name or "<unnamed>")

        def __str__(self) -> str:
            self._record()
            return self._FILL

        def __bool__(self) -> bool:
            # `{% if undefined %}` is an error under Ansible's Templar, so it is one here too.
            # `x | default(y)` and `x is defined` inspect the type without going through this,
            # which is why those idioms are not flagged.
            self._record()
            return False

        def __iter__(self):
            self._record()
            return iter(())

        def __add__(self, other):  # {{ secret | indent(n) }}
            return str(self) + str(other)

        def __radd__(self, other):
            return str(other) + str(self)

    return Tracking


def secret_names(registry: Path = SECRET_REGISTRY) -> frozenset[str]:
    """Every SOPS secret name, from the plaintext rotation registry.

    Names only — the registry holds no values, which is why it is readable here at all.
    """
    return frozenset(load_yaml(registry).get("entries") or {})


def inventory_names() -> frozenset[str]:
    """Every variable name the plaintext inventory defines, group_vars plus every host."""
    names = set(load_yaml(ALL_VARS))
    for host in host_files():
        names.update(load_yaml(host))
    return frozenset(names)


def is_allowed_undefined(name: str, known: frozenset[str]) -> bool:
    """Whether an undefined variable is one this guard tolerates.

    `known` is the union of the secret registry and the inventory. The host renderer's own
    names are matched here rather than folded into it, because no file this repo could census
    holds them.
    """
    root = name.split(".", 1)[0].split("[", 1)[0]
    return root in HOST_RENDER_SUPPLIED or root in known


def discover_templates(components: Path = COMPONENTS) -> list[Path]:
    """Every host component template under its `templates/`, shell templates excepted.

    `shell_templates.py` renders each `*.sh.j2` with the host context it needs.
    """
    return sorted(
        tpl
        for tpl in components.glob("*/templates/*.j2")
        if not tpl.name.endswith(".sh.j2")
    )


def template_loader(tpl: Path) -> tuple[Path, str]:
    """The directory `tpl`'s environment is rooted at, and the name it loads by.

    A component template loads from the component, as the host renderer loads it, so its
    `templates/<name>` imports resolve.
    """
    if (tpl.parents[1] / "component.yml").is_file():
        return tpl.parents[1], f"templates/{tpl.name}"
    return tpl.parent, tpl.name


def repo_relative(tpl: Path) -> str:
    """A short label for one template: repo-relative where it is in the tree, else its path.

    The tests hand `check_template` a template written under `tmp_path`, which is outside the
    repo — `Path.relative_to` raises there rather than returning something printable.
    """
    for root in (ANSIBLE, REPO):
        if tpl.is_relative_to(root):
            return str(tpl.relative_to(root))
    return str(tpl)


def check_template(tpl: Path, ctx: dict, known: frozenset[str]) -> list[str]:
    """Render one template and return its problems (empty on success).

    Reports a render failure, then every variable nothing defines, then a YAML parse error for
    the templates that are YAML.
    """
    rel = repo_relative(tpl)
    seen: dict[str, set[str]] = defaultdict(set)
    root, name = template_loader(tpl)
    env = template_env(root, undefined_cls=_tracking_undefined(seen, rel))
    rendered, err = render_or_error(env, name, ctx)
    if rendered is None:
        return [err or "render error"]

    problems = [
        f"undefined variable {name!r} — nothing in the inventory, the component defaults, the "
        f"secret registry or a component spec defines it"
        for name in sorted(seen[rel])
        if not is_allowed_undefined(name, known)
    ]

    if tpl.name.endswith((".yaml.j2", ".yml.j2")):
        try:
            list(yaml_fast.safe_load_all(rendered))
        except yaml.YAMLError as exc:
            print(f"\n----- rendered {rel} -----", file=sys.stderr)
            dump_numbered(rendered)
            problems.append(f"invalid YAML: {exc}")
    return problems


def component_supplied(name: str, components: Path = COMPONENTS) -> frozenset[str]:
    """Variable names the host components rendering component `name`'s templates supply.

    The component itself, and each component that borrows it with `component:`, render its
    templates with their own `defaults:` and, for each secret key an output declares,
    `<key>_value`.
    """
    out: set[str] = set()
    for path in sorted(components.glob("*/component.yml")):
        spec = yaml_fast.safe_load(path.read_text()) or {}
        if name not in (spec.get("component"), path.parent.name):
            continue
        out.update(spec.get("defaults") or {})
        for output in spec.get("outputs") or []:
            out.update(f"{key}_value" for key in output.get("keys") or [])
    return frozenset(out)


def component_context(directory: Path, overrides: dict) -> dict:
    """The context `directory`'s templates render with.

    Its `defaults/main.yml` under the inventory, `overrides` on top, and its `component.yml`
    `defaults:` filling any name still unset.
    """
    ctx = render_context(directory, overrides=overrides)
    spec = directory / "component.yml"
    if spec.is_file():
        for key, value in (load_yaml(spec).get("defaults") or {}).items():
            ctx.setdefault(key, value)
    return ctx


def check_role_template(
    tpl: Path, known: frozenset[str], components: Path = COMPONENTS
) -> list[str]:
    """`check_template` under the context a deploy renders `tpl` with.

    The owning component's `defaults/main.yml` under the inventory, with its `component.yml`
    `defaults:` filling any name the inventory leaves unset, minus the secrets and inventory names
    `known` covers. The default names join `known`, because a default whose value will not expand
    is left out of the context and still counts as defined. `components` is where
    `component_supplied` looks, a parameter for the tests.
    """
    ctx = component_context(tpl.parents[1], HOST_RENDER_CONTEXT)
    own = tpl.parents[1] / "defaults" / "main.yml"
    defaults = frozenset(load_yaml(own) if own.is_file() else ())
    return check_template(
        tpl, ctx, known | defaults | component_supplied(tpl.parents[1].name, components)
    )


def main(argv: Sequence[str] = ()) -> int:
    """Render every host component template and report unresolved variables and bad YAML.

    Args:
        argv: The command-line arguments. `-v` or `--verbose` also lists each clean
            template as an `[ok]` line; by default only failures and the summary print.

    Returns:
        0 if every template rendered clean, 1 if any failed.
    """
    answer_help(__doc__, list(argv))
    ok = ok_printer(argv)
    known = secret_names() | inventory_names()
    templates = discover_templates()
    failures = 0
    for tpl in templates:
        rel = repo_relative(tpl)
        problems = check_role_template(tpl, known)
        if problems:
            failures += 1
            for problem in problems:
                print(f"  [FAIL] {rel}: {problem}", file=sys.stderr)
        else:
            ok(f"  [ok]   {rel}")
    print(f"\n{len(templates)} component template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
