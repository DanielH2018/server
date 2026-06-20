"""Render Home Assistant custom_templates macros in a runtime-free Jinja2 environment that
faithfully mirrors the handful of HA filter overrides the macros use.

HA's template engine IS Jinja2 (an ImmutableSandboxedEnvironment) but HA replaces several stock
filters with its own `forgiving_*` versions. The bedroom macros use float / int / round / bool;
this shim reproduces HA's semantics for exactly those, so the unit tests agree with production.

The load-bearing one: HA's `round` (forgiving_round) uses Python's banker's rounding
(round-half-to-EVEN) and returns an int at precision 0. Jinja's STOCK `round` rounds half away
from zero and returns a float. fan.jinja's level math lands on .5 midpoints by design, so this
difference would silently corrupt the tests if we used a bare Jinja2 env. Pinned by
test_ha_round_semantics.py.
"""
import math
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_MACRO_DIR = Path(__file__).resolve().parent.parent / "files" / "custom_templates"
_SENTINEL = object()


def _forgiving_float(value, default=0.0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _forgiving_int(value, default=0, base=10):
    try:
        return int(value)
    except (ValueError, TypeError):
        try:
            return int(str(value), base)
        except (ValueError, TypeError):
            return default


def _forgiving_round(value, precision=0, method="common", default=_SENTINEL):
    try:
        value = float(value)
        if method == "ceil":
            value = math.ceil(value * 10 ** precision) / 10 ** precision
        elif method == "floor":
            value = math.floor(value * 10 ** precision) / 10 ** precision
        elif method == "half":
            value = round(value * 2) / 2
        else:  # "common" -> Python round = banker's rounding, matching HA
            value = round(value, precision)
        return int(value) if precision == 0 else value
    except (ValueError, TypeError):
        return value if default is _SENTINEL else default


_TRUE = {"true", "yes", "on", "enable", "1"}
_FALSE = {"false", "no", "off", "disable", "0", "none", ""}


def _forgiving_bool(value, default=_SENTINEL):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return bool(value) if default is _SENTINEL else default


def _env():
    env = Environment(loader=FileSystemLoader(str(_MACRO_DIR)))
    env.filters["float"] = _forgiving_float
    env.filters["int"] = _forgiving_int
    env.filters["round"] = _forgiving_round
    env.filters["bool"] = _forgiving_bool
    return env


def render_macro(file: str, macro: str, *args) -> str:
    """Render `{% from file import macro %}{{ macro(*args) }}` and return the stripped result.

    Python scalars are passed as native Jinja context variables (so floats stay floats and bools
    stay bools), and the macro is invoked positionally.
    """
    env = _env()
    ctx = {f"a{i}": v for i, v in enumerate(args)}
    call = ", ".join(f"a{i}" for i in range(len(args)))
    template = env.from_string(
        "{%% from '%s' import %s %%}{{ %s(%s) }}" % (file, macro, macro, call)
    )
    return template.render(**ctx).strip()
