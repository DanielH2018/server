#!/usr/bin/env python3
"""Render every setup-plane Jinja template and fail on a variable nothing defines.

The other render guards stop short of `ansible/roles/setup/`: `config_templates.py` reads
`roles/containers`, `k8s_manifests.py` reads `roles/k8s`, `shell_templates.py` covers `*.sh.j2`
anywhere and `unit_templates.py` covers `*.service.j2` / `*.timer.j2`. Nothing rendered a
setup-plane `*.yaml.j2`, `*.env.j2`, `*.conf.j2` or Corefile, and `ansible-lint` lints the task
that calls `lookup('template', ...)`, never the template's own render. A setup template with an
undefined variable, a Jinja syntax error, or a rename its consumer missed therefore passed the
whole gate green (GitHub issue #1468).

The setup plane is hand-applied by construction, which makes the gap worse rather than better:
no routine deploy exercises it, so the first run of a bad template is a bring-up someone reaches
for during an incident. `roles/setup/k3s/templates/longhorn-b2-secret.yaml.j2` is the worked
example — it renders Longhorn's B2 credential, and an undefined variable there produces an empty
`AWS_ACCESS_KEY_ID` rather than an error, surfacing as backups silently not working.

WHAT COUNTS AS UNDEFINED. Rendering with a bare `StrictUndefined` would fail on every SOPS
secret and every fact a task supplies at runtime, so this guard renders with a tracking
Undefined and then judges each name it collected. A name is allowed to be undefined when it is:

  - Ansible-supplied — `ansible_*`, `inventory_hostname`, `hostvars`, `groups`, `item`
    (`ANSIBLE_SUPPLIED` / `ANSIBLE_SUPPLIED_PREFIXES`);
  - a SOPS secret, by name, from the plaintext registry `ansible/secret_rotation.yml`;
  - runtime-supplied by a setup-plane task — a `set_fact` key, a task-level `vars:` key, or a
    `register:` name, derived from the tasks themselves (`runtime_vars`), never listed;
  - defined anywhere in the inventory — `group_vars/all.yml` or any host's `host_vars` file.

Everything else is a failure naming the template and the variable. That is the shape a rename
takes: the producer moves and the consumer keeps the old name, which then matches none of the
four sources above.

The YAML templates (`*.yaml.j2` / `*.yml.j2`) are additionally parsed, the same structural check
the other three guards make. The rest are rendered only — a `.env`, a Corefile and libvirt XML
have no shared parser, and their shell/unit siblings already have validators of their own.

Run directly or via the ``validate-setup-templates`` prek hook. Exits non-zero on any render
failure, unresolved variable, or invalid YAML.
"""

import sys
from collections import defaultdict
from pathlib import Path

import yaml
from jinja2 import ChainableUndefined

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from ansible.plugins.filter.core import comment, to_bool, to_uuid

from lib import yaml_fast
from lib.render_guard import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    SHARED_TPL,
    dump_numbered,
    host_files,
    load_yaml,
    make_env,
    render_or_error,
)
from lib.repo_paths import ROLES

SETUP = ROLES / "setup"
SECRET_REGISTRY = ANSIBLE / "secret_rotation.yml"

# Names Ansible itself supplies, which no repo file defines and none should.
ANSIBLE_SUPPLIED = frozenset(
    {
        "ansible_managed",
        "hostvars",
        "groups",
        "inventory_hostname",
        "item",
        "playbook_dir",
    }
)
ANSIBLE_SUPPLIED_PREFIXES = ("ansible_",)

SET_FACT_KEYS = frozenset({"set_fact", "ansible.builtin.set_fact"})

# Ansible-supplied values given a plausible rendering rather than a stub: the `comment` filter
# raises on an Undefined, so `{{ ansible_managed | comment }}` cannot render without one.
ANSIBLE_RUNTIME_CONTEXT = {
    "ansible_managed": "Ansible managed",
    "inventory_hostname": "daniel-box",
}


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


def _walk(node, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            # Both spellings: this repo writes `ansible.builtin.set_fact`, and matching the
            # bare name alone silently missed every one of them — the whole derivation
            # returned only the handful written short.
            if key in SET_FACT_KEYS and isinstance(value, dict):
                out.update(k for k in value if k != "cacheable")
            elif key == "vars" and isinstance(value, dict):
                out.update(value)
            elif key == "register" and isinstance(value, str):
                out.add(value)
            _walk(value, out)
    elif isinstance(node, list):
        for entry in node:
            _walk(entry, out)


def runtime_vars(setup: Path = SETUP) -> frozenset[str]:
    """Variable names the setup-plane tasks supply at runtime.

    A `set_fact` key, a task-level `vars:` key, or a `register:` name. Derived from every
    `tasks/**/*.yml` under the setup roles rather than listed, so renaming the producer without
    the consumer is exactly what stops matching. Deliberately plane-wide rather than per-role:
    `roles/setup/common/templates/resolv.conf.j2` reads `common_resolver_nameservers`, which the
    k3s and optimize_pi roles set before including it.
    """
    out: set[str] = set()
    for tasks in sorted(setup.glob("*/tasks/**/*.yml")):
        try:
            _walk(yaml_fast.safe_load(tasks.read_text()), out)
        except yaml.YAMLError:
            # A task file that does not parse is `check-yaml`'s and ansible-lint's problem, not
            # this guard's. Skipping it can only widen the failure list, never narrow it.
            continue
    return frozenset(out)


def inventory_names() -> frozenset[str]:
    """Every variable name the plaintext inventory defines, group_vars plus every host."""
    names = set(load_yaml(ALL_VARS))
    for host in host_files():
        names.update(load_yaml(host))
    return frozenset(names)


def is_allowed_undefined(name: str, known: frozenset[str]) -> bool:
    """Whether an undefined variable is one this guard tolerates.

    `known` is the union of the secret registry, the runtime-supplied names and the inventory;
    Ansible's own magic vars are matched here rather than folded into it, because they come from
    the runtime rather than from any file this repo could census.
    """
    root = name.split(".", 1)[0].split("[", 1)[0]
    if root in ANSIBLE_SUPPLIED or root.startswith(ANSIBLE_SUPPLIED_PREFIXES):
        return True
    return root in known


def discover_templates(setup: Path = SETUP) -> list[Path]:
    """Every setup-plane template, one level under a role's `templates/`."""
    return sorted(setup.glob("*/templates/*.j2"))


def build_env(template_dir: Path, undefined_cls):
    """The render environment, carrying the Ansible filters the setup templates reach for.

    `bool` is ansible-core's own `to_bool` for the reason `k8s_manifests.register_ansible_filters`
    gives: `bool("false")` is True in Python, so a hand-rolled shim would take the opposite
    branch from a real deploy. `comment` and `to_uuid` are likewise the real implementations —
    `to_uuid` is a deterministic UUIDv5, so a stub would render a different file every run.
    """
    env = make_env([template_dir, SHARED_TPL], undefined_cls=undefined_cls)
    env.filters["bool"] = to_bool
    env.filters["comment"] = comment
    env.filters["to_uuid"] = to_uuid
    return env


def repo_relative(tpl: Path) -> str:
    """A short label for one template: repo-relative where it is in the tree, else its path.

    The tests hand `check_template` a template written under `tmp_path`, which is outside the
    repo — `Path.relative_to` raises there rather than returning something printable.
    """
    try:
        return str(tpl.relative_to(ANSIBLE))
    except ValueError:
        return str(tpl)


def check_template(tpl: Path, ctx: dict, known: frozenset[str]) -> list[str]:
    """Render one template and return its problems (empty on success).

    Reports a render failure, then every variable nothing defines, then a YAML parse error for
    the templates that are YAML.
    """
    rel = repo_relative(tpl)
    seen: dict[str, set[str]] = defaultdict(set)
    env = build_env(tpl.parent, _tracking_undefined(seen, rel))
    rendered, err = render_or_error(env, tpl.name, ctx)
    if rendered is None:
        return [err or "render error"]

    problems = [
        f"undefined variable {name!r} — nothing in the inventory, the role defaults, the "
        f"secret registry or a setup-plane task defines it"
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


def main() -> int:
    """Render every setup-plane template and report unresolved variables and bad YAML.

    Returns:
        0 if every template rendered clean, 1 if any failed.
    """
    base = {**BASE_CONTEXT, **ANSIBLE_RUNTIME_CONTEXT, **load_yaml(ALL_VARS)}
    known = secret_names() | runtime_vars() | inventory_names()
    templates = discover_templates()
    failures = 0
    for tpl in templates:
        # The owning role's defaults, layered over the inventory — the same context Ansible
        # gives the task that renders it, minus the secrets and runtime facts `known` covers.
        role = tpl.parents[1]
        ctx = {**base, **load_yaml(role / "defaults" / "main.yml")}
        rel = str(tpl.relative_to(ANSIBLE))
        problems = check_template(tpl, ctx, known)
        if problems:
            failures += 1
            for problem in problems:
                print(f"  [FAIL] {rel}: {problem}", file=sys.stderr)
        else:
            print(f"  [ok]   {rel}")
    print(f"\n{len(templates)} setup template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
