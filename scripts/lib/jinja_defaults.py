#!/usr/bin/env python3
"""Resolve a generated doc's `{{ … }}` to the value every host would get, or leave it as written.

The glance blocks and `docs/reference/crons.md` print the resolved value wherever one
exists. When a variable comes from the role's
`defaults/main.yml` or `group_vars/all.yml`, and no `host_vars` file sets it, every host in
the inventory resolves it to the same value. Printing `{{ '%02d' |
format(gitops_deploy_ruleset_drift_cron_hour | int) }}` then hides a fixed value behind
syntax a reader has to evaluate by hand.

An expression resolves only when all of these hold; otherwise it is printed as written,
which keeps the old guarantee for every case that really does vary:

- Every variable it names has a value in role defaults or `group_vars/all.yml`.
- No `host_vars/*.yml` sets any of those variables at the top level.
- No such value is itself a template.
- It evaluates with Jinja's builtin filters under `StrictUndefined`. An Ansible-only filter
  raises, and the expression stays as written.

Imported as `from lib.jinja_defaults import resolve` after the caller's own `sys.path`
bootstrap puts `scripts/` on the path (`.claude/rules/python-layout.md`).
"""

import re
import sys as _sys
from functools import lru_cache
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined, meta

from lib import yaml_fast
from lib.repo_paths import INVENTORY

_EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)
_ENV = Environment(undefined=StrictUndefined)


def _top_level(path: Path) -> dict:
    try:
        data = yaml_fast.safe_load(path.read_text())
    except OSError, yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=None)
def _inventory_values(inventory: Path) -> tuple[dict, frozenset]:
    """(`group_vars/all.yml`'s values, the names any `host_vars` file overrides)."""
    shared = _top_level(inventory / "group_vars" / "all.yml")
    overridden: set[str] = set()
    for host_file in sorted((inventory / "host_vars").glob("*.yml")):
        overridden.update(_top_level(host_file))
    return shared, frozenset(overridden)


def _one(expr: str, values: dict, overridden: frozenset) -> str | None:
    """The rendered value of one `{{ expr }}`, or None when it must stay as written."""
    source = "{{" + expr + "}}"
    try:
        names = meta.find_undeclared_variables(_ENV.parse(source))
    except Exception:
        return None
    if not names or names & overridden:
        return None
    if any(n not in values or "{{" in str(values[n]) for n in names):
        return None
    try:
        return _ENV.from_string(source).render({n: values[n] for n in names})
    except Exception:
        return None


def resolve(text: str, role_dir: Path, inventory: Path = INVENTORY) -> str:
    """`text` with each `{{ … }}` replaced by its fleet-wide value where that is safe.

    Args:
        text: A schedule, a timer key or another fragment copied from a task or template.
        role_dir: The role whose `defaults/main.yml` supplies the lowest-precedence values.
        inventory: The inventory root; tests pass a fixture tree.

    Returns:
        The text with every resolvable expression rendered and every other one unchanged.
    """
    if "{{" not in text:
        return text
    shared, overridden = _inventory_values(inventory)
    values = {**_top_level(role_dir / "defaults" / "main.yml"), **shared}

    def replace(match: re.Match) -> str:
        rendered = _one(match.group(1), values, overridden)
        return match.group(0) if rendered is None else rendered

    return _EXPR.sub(replace, text)
