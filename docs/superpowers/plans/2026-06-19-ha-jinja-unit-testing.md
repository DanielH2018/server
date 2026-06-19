# Home Assistant Jinja Logic Unit Testing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fast, runtime-free `pytest` suite for the bedroom HA Jinja logic (fan curve, level round-trip, wake ramp, lux gate) by extracting the math into shared `custom_templates` macros and testing those macros directly.

**Architecture:** Pull the bug-prone inline Jinja math out of `scripts.yaml`/`templates.yaml` into pure macros in `files/custom_templates/` (entity reads stay in the YAML callers; macros take plain numbers → numbers/bools). Test the macros with a tiny Jinja2 harness that faithfully mirrors HA's `forgiving_round`/`float`/`int`/`bool` filter overrides. Wire the suite into `pyproject.toml` `testpaths`, switch the `custom_templates` deploy to a directory copy, then deploy and verify live.

**Tech Stack:** Python 3 + `pytest` (via `uv`), Jinja2, Ansible, Home Assistant (LSIO image).

## Global Constraints

- **`containers/` is read-only** — edit only the Ansible role sources under `ansible/roles/containers/home-assistant/`.
- **HA Jinja files ship via `copy`, never `template`** — they use HA `{{ }}` Jinja that Ansible's templater would mangle.
- **`testpaths` in `pyproject.toml` is the single source of truth** for what `uv run pytest`, the prek `pytest` hook, and CI run.
- **Tests must NOT live under `ansible/filter_plugins/`** — Ansible's plugin loader imports every `.py` there at deploy time.
- **HA's `round` is banker's rounding** (`forgiving_round`, Python `round`, returns int at precision 0) — NOT Jinja's stock half-away-from-zero float. The harness must reproduce this.
- **Macro extraction is a pure refactor** — behavior must be byte-for-byte preserved; verify old-inline-vs-new-macro equality before deleting any inline formula.
- **Commit style:** end commit messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Stay on `master`, no feature branch.
- **Run tests with:** `uv run pytest ansible/roles/containers/home-assistant/tests -v`.

---

## File Structure

- `ansible/roles/containers/home-assistant/files/custom_templates/fan.jinja` — **modify**: add `fan_target_level` macro.
- `ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja` — **create**: `in_wake_window`, `wake_brightness`, `wake_transition`, `auto_light_allowed`.
- `ansible/roles/containers/home-assistant/tests/jinja_harness.py` — **create**: HA-faithful Jinja2 render helper + `forgiving_*` filters.
- `ansible/roles/containers/home-assistant/tests/test_ha_round_semantics.py` — **create**: pin banker's rounding.
- `ansible/roles/containers/home-assistant/tests/test_fan_macros.py` — **create**: round-trip, curve, hysteresis, caps.
- `ansible/roles/containers/home-assistant/tests/test_lighting_macros.py` — **create**: wake ramp, window, lux gate.
- `ansible/roles/containers/home-assistant/files/scripts.yaml` — **modify**: rewire `bedroom_apply_fan` + `bedroom_apply_natural`.
- `ansible/roles/containers/home-assistant/files/templates.yaml` — **modify**: rewire `bedroom_auto_light_allowed`.
- `ansible/roles/containers/home-assistant/tasks/main.yml` — **modify**: directory copy for `custom_templates`.
- `pyproject.toml` — **modify**: add the tests dir to `testpaths`.

---

### Task 1: HA-faithful Jinja harness + round-semantics pin

Build the test harness first — every macro test depends on it, and its `round` fidelity is the linchpin of the whole suite.

**Files:**
- Create: `ansible/roles/containers/home-assistant/tests/jinja_harness.py`
- Test: `ansible/roles/containers/home-assistant/tests/test_ha_round_semantics.py`

**Interfaces:**
- Produces: `render_macro(file: str, macro: str, *args) -> str` — renders `{% from file import macro %}{{ macro(*args) }}` against the role's `files/custom_templates/` dir, passing Python scalars as native Jinja context values, returns the stripped string.
- Produces: `_forgiving_round(value, precision=0, method="common", default=_SENTINEL)`, `_forgiving_float(value, default=0.0)`, `_forgiving_int(value, default=0, base=10)`, `_forgiving_bool(value)` — module-level functions, also registered as the env's `round`/`float`/`int`/`bool` filters.

- [ ] **Step 1: Write the harness module**

Create `ansible/roles/containers/home-assistant/tests/jinja_harness.py`:

```python
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
```

- [ ] **Step 2: Write the failing round-semantics test**

Create `ansible/roles/containers/home-assistant/tests/test_ha_round_semantics.py`:

```python
"""Pin HA's banker's-rounding semantics so the Jinja harness can never silently drift from
Home Assistant's forgiving_round (which fan.jinja's .5-midpoint level math depends on)."""
from jinja_harness import _forgiving_round


def test_half_rounds_to_even_not_away_from_zero():
    # Banker's rounding: ties go to the nearest EVEN integer.
    assert _forgiving_round(0.5) == 0
    assert _forgiving_round(1.5) == 2
    assert _forgiving_round(2.5) == 2
    assert _forgiving_round(3.5) == 4


def test_returns_int_at_precision_zero():
    assert isinstance(_forgiving_round(1.4), int)
    assert _forgiving_round(1.4) == 1
    assert _forgiving_round(1.6) == 2


def test_returns_float_with_precision():
    assert _forgiving_round(1.2345, 2) == 1.23
    assert isinstance(_forgiving_round(1.2345, 2), float)
```

- [ ] **Step 3: Run the test to verify it passes**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_ha_round_semantics.py -v`
Expected: PASS (3 tests). If `import jinja_harness` fails, confirm the test file and `jinja_harness.py` are in the same directory (pytest's default `prepend` import mode puts that dir on `sys.path`).

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/containers/home-assistant/tests/jinja_harness.py \
        ansible/roles/containers/home-assistant/tests/test_ha_round_semantics.py
git commit -m "$(printf 'test(home-assistant): HA-faithful Jinja macro harness\n\nRender custom_templates macros in a bare Jinja2 env with forgiving_round/\nfloat/int/bool shims mirroring HA. Banker round pinned.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 2: Extract `fan_target_level` macro + tests

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/custom_templates/fan.jinja`
- Test: `ansible/roles/containers/home-assistant/tests/test_fan_macros.py`

**Interfaces:**
- Consumes: `render_macro` from Task 1.
- Produces (HA Jinja macro): `fan_target_level(temp_f, cur_level, is_night, sleep)` → integer string `"0".."9"`. Reproduces `bedroom_apply_fan`'s inline `ideal`/`cap`/`want`/`target_level` math verbatim.

- [ ] **Step 1: Write the failing fan-macro tests**

Create `ansible/roles/containers/home-assistant/tests/test_fan_macros.py`:

```python
"""Unit tests for the DREO fan macros in custom_templates/fan.jinja."""
from jinja_harness import render_macro

FAN = "fan.jinja"


def _level(pct):
    return int(render_macro(FAN, "pct_to_level", pct))


def _pct(level):
    return int(render_macro(FAN, "level_to_pct", level))


def _target(temp_f, cur_level, is_night, sleep):
    return int(render_macro(FAN, "fan_target_level", temp_f, cur_level, is_night, sleep))


def test_level_pct_roundtrip_never_drifts():
    # The fan.jinja promise: send the midpoint % for a level, read it back, get the same level.
    for level in range(0, 10):
        assert _level(_pct(level)) == level, f"round-trip drifted at level {level}"


def test_off_below_start_temperature():
    assert _target(71.0, 0, False, False) == 0   # ideal 0 -> off
    assert _target(70.0, 3, False, False) == 0   # cold even with a fan already running


def test_unavailable_sensor_sentinel_is_off():
    assert _target(-1.0, 5, False, False) == 0   # t < 0 -> off (sensor unavailable)


def test_curve_low_and_high_ends():
    assert _target(72.0, 0, False, False) == 1   # (72-71)/1.3 = 0.77 -> 1
    assert _target(83.0, 0, False, False) == 9   # (83-71)/1.3 = 9.23 -> 9


def test_curve_clamps_at_max_level():
    assert _target(90.0, 0, False, False) == 9   # ideal ~14.6, capped to 9


def test_hysteresis_holds_within_deadband():
    # ideal 5.4 with cur_level 5 is within +/-0.7 -> no step.
    assert _target(78.02, 5, False, False) == 5


def test_hysteresis_steps_outside_deadband():
    # ideal 5.85 with cur_level 5 exceeds +0.7 -> step up.
    assert _target(78.6, 5, False, False) == 6


def test_sleep_cap_limits_to_level_2():
    assert _target(83.0, 0, False, True) == 2


def test_night_cap_limits_to_level_4():
    assert _target(83.0, 0, True, False) == 4
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_fan_macros.py -v`
Expected: FAIL — `test_level_pct_roundtrip_*` may pass (existing macros), but every `_target` test fails because `fan_target_level` is not defined (Jinja `TemplateAssertionError`).

- [ ] **Step 3: Add the macro to fan.jinja**

Append to `ansible/roles/containers/home-assistant/files/custom_templates/fan.jinja`:

```jinja

{# Temperature(°F) + current level + caps -> target DREO level (0..9). The single source of truth
   for the fan curve, extracted verbatim from bedroom_apply_fan so it can be unit-tested: the
   (t-71)/1.3 ideal curve, the ±0.7-level hysteresis deadband, and the night/sleep caps. Entity
   reads (temp, current %, clock, sleep flag) stay in the caller. TUNE the curve here: the 71 offset
   sets the start temp; the /1.3 divisor sets the slope (bigger = gentler). Tested in
   tests/test_fan_macros.py. #}
{%- macro fan_target_level(temp_f, cur_level, is_night, sleep) -%}
{%- set t = temp_f | float(-1) -%}
{%- set cl = cur_level | int(0) -%}
{%- set ideal = ((t - 71) / 1.3) if t >= 0 else 0 -%}
{%- set cap = (2 if (sleep | bool) else (4 if (is_night | bool) else 9)) | int -%}
{%- set want = 0 if (t < 0 or ideal < 0.3)
   else ((ideal | round | int) if (cl == 0 or ideal > cl + 0.7 or ideal < cl - 0.7) else cl) -%}
{{ [want, cap] | min | int }}
{%- endmacro -%}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_fan_macros.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/custom_templates/fan.jinja \
        ansible/roles/containers/home-assistant/tests/test_fan_macros.py
git commit -m "$(printf 'test(home-assistant): extract + test fan_target_level macro\n\nMove the fan curve/hysteresis/caps math into fan.jinja as a pure macro\nand cover round-trip, curve ends, hysteresis, and caps.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 3: Create `lighting.jinja` macros + tests

**Files:**
- Create: `ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja`
- Test: `ansible/roles/containers/home-assistant/tests/test_lighting_macros.py`

**Interfaces:**
- Consumes: `render_macro` from Task 1.
- Produces (HA Jinja macros in `lighting.jinja`):
  - `in_wake_window(elapsed_min)` → `"True"`/`"False"` (`0 <= elapsed_min < 15`; negative sentinel → False).
  - `wake_brightness(elapsed_min, sleep_min)` → integer string; `1` at elapsed 0, `peak` at 15, where `peak = 30 if 0 < sleep_min < 360 else 50`.
  - `wake_transition(elapsed_min)` → integer string seconds, `(15 - elapsed_min) * 60`.
  - `auto_light_allowed(in_window, illuminance)` → `"True"`/`"False"` (`in_window | bool` OR `illuminance < 50`).

- [ ] **Step 1: Write the failing lighting-macro tests**

Create `ansible/roles/containers/home-assistant/tests/test_lighting_macros.py`:

```python
"""Unit tests for the bedroom lighting macros in custom_templates/lighting.jinja."""
from jinja_harness import render_macro

LIGHT = "lighting.jinja"


def _window(elapsed):
    return render_macro(LIGHT, "in_wake_window", elapsed)


def _brightness(elapsed, sleep_min):
    return int(render_macro(LIGHT, "wake_brightness", elapsed, sleep_min))


def _transition(elapsed):
    return int(render_macro(LIGHT, "wake_transition", elapsed))


def _allowed(in_window, illuminance):
    return render_macro(LIGHT, "auto_light_allowed", in_window, illuminance)


def test_in_wake_window_boundaries():
    assert _window(0) == "True"
    assert _window(7.5) == "True"
    assert _window(14.99) == "True"
    assert _window(15) == "False"      # strict upper bound (window ends AT the alarm)
    assert _window(-1) == "False"      # unavailable-sensor sentinel


def test_wake_brightness_ramp_endpoints():
    assert _brightness(0, 0) == 1      # 1% at window start
    assert _brightness(15, 0) == 50    # full peak at the alarm (normal night)


def test_wake_brightness_short_night_lowers_peak():
    assert _brightness(15, 300) == 30  # 0 < 300 < 360 -> gentler 30% peak
    assert _brightness(15, 0) == 50    # unknown/0 sleep -> normal 50%
    assert _brightness(15, 400) == 50  # long night -> normal 50%


def test_wake_brightness_is_monotonic():
    vals = [_brightness(e, 0) for e in range(0, 16)]
    assert vals == sorted(vals)
    assert vals[0] == 1 and vals[-1] == 50


def test_wake_transition_counts_down_seconds():
    assert _transition(0) == 900       # full 15 min remaining
    assert _transition(7.5) == 450
    assert _transition(15) == 0


def test_auto_light_allowed_truth_table():
    assert _allowed(True, 1000) == "True"   # in-window wakes regardless of brightness
    assert _allowed(False, 40) == "True"    # dark enough
    assert _allowed(False, 49) == "True"
    assert _allowed(False, 50) == "False"   # strict < 50
    assert _allowed(False, 60) == "False"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_lighting_macros.py -v`
Expected: FAIL — `lighting.jinja` does not exist (`TemplateNotFound`).

- [ ] **Step 3: Create lighting.jinja**

Create `ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja`:

```jinja
{# Shared bedroom lighting math. Entity reads (sensor.bedroom_wake_start, now(), illuminance) stay
   in the callers; these macros are pure so they are unit-tested in tests/test_lighting_macros.py.
   HA loads custom_templates/*.jinja at startup; this deploy recreates the container, so edits take
   effect on deploy. Live edit: Developer Tools -> Actions -> homeassistant.reload_custom_templates. #}

{# Minutes elapsed into the 15-min morning wake window -> is the wake ramp active? The caller passes
   the minutes since sensor.bedroom_wake_start (or a negative sentinel when the sensor is
   unavailable). SINGLE source of truth for the window [0, 15), shared by bedroom_apply_natural's
   nightlight + wake exceptions and templates.yaml's bedroom_auto_light_allowed. #}
{%- macro in_wake_window(elapsed_min) -%}
{%- set e = elapsed_min | float(-1) -%}
{{ 0 <= e < 15 }}
{%- endmacro -%}

{# Wake-ramp brightness %: 1% at window start (elapsed 0) ramping linearly to `peak` at the alarm
   (elapsed 15). Sleep-aware peak: 30% after a short night (0 < sleep_min < 360), else 50%
   (unknown/0/long -> 50). #}
{%- macro wake_brightness(elapsed_min, sleep_min) -%}
{%- set e = elapsed_min | float(0) -%}
{%- set s = sleep_min | float(0) -%}
{%- set peak = 30 if (0 < s < 360) else 50 -%}
{{ (1 + (peak - 1) * e / 15) | round(0) | int }}
{%- endmacro -%}

{# Wake-ramp transition (seconds): the minutes remaining in the 15-min window, as seconds. #}
{%- macro wake_transition(elapsed_min) -%}
{%- set e = elapsed_min | float(0) -%}
{{ ((15 - e) * 60) | round(0) | int }}
{%- endmacro -%}

{# Auto-light gate: dark enough OR inside the wake window. illuminance < 50 lux is "dark"; in_window
   forces allow for the morning ramp regardless of ambient. SINGLE source of truth for the 50-lux
   threshold. in_window is coerced with `| bool` because a macro argument arrives as a string when
   passed a macro's rendered output. #}
{%- macro auto_light_allowed(in_window, illuminance) -%}
{%- set iw = in_window | bool -%}
{%- set lux = illuminance | float(9999) -%}
{{ iw or lux < 50 }}
{%- endmacro -%}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_lighting_macros.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/custom_templates/lighting.jinja \
        ansible/roles/containers/home-assistant/tests/test_lighting_macros.py
git commit -m "$(printf 'test(home-assistant): add + test lighting.jinja macros\n\nwake_brightness/wake_transition/in_wake_window/auto_light_allowed as pure\nmacros, covered for ramp endpoints, window bounds, and the lux gate.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 4: Wire the suite into pyproject + the custom_templates directory copy

Two independent infra changes that gate the suite into CI and ship the new macro file. Folded into one task because neither has its own test cycle and both must land before the live deploy.

**Files:**
- Modify: `pyproject.toml` (the `[tool.pytest.ini_options]` `testpaths` list)
- Modify: `ansible/roles/containers/home-assistant/tasks/main.yml:80-86`

**Interfaces:**
- Consumes: the tests dir from Tasks 1-3.
- Produces: the suite running under `uv run pytest` (no path arg) and the prek hook; `lighting.jinja` shipped to `/config/custom_templates/`.

- [ ] **Step 1: Add the tests dir to testpaths**

In `pyproject.toml`, inside `[tool.pytest.ini_options]` `testpaths`, add the line after the existing `renovate_notify` entry:

```toml
  "ansible/roles/setup/renovate_notify/files", # notifier decision tests
  "ansible/roles/containers/home-assistant/tests", # HA Jinja macro logic tests
```

- [ ] **Step 2: Verify the full suite collects the new tests**

Run: `uv run pytest -q`
Expected: PASS — the run now includes `test_ha_round_semantics`, `test_fan_macros`, `test_lighting_macros` alongside the existing suites (no path argument needed).

- [ ] **Step 3: Switch the custom_templates deploy task to a directory copy**

In `ansible/roles/containers/home-assistant/tasks/main.yml`, replace the "Deploy custom Jinja templates" task (currently hardcoding `src: custom_templates/fan.jinja`) with a whole-directory copy so any `custom_templates/*.jinja` ships automatically:

```yaml
# Shared HA Jinja macros (custom_templates/*.jinja), imported by automations/scripts/templates.
# Whole-directory copy (HA Jinja, not Ansible-templated) so new macro files ship automatically —
# same copy-not-template reason as automations/scenes/scripts.
- name: Deploy custom Jinja templates from static files
  tags: [config]
  ansible.builtin.copy:
    src: custom_templates/
    dest: "/home/{{ sys_user }}/server/containers/{{ container_item.name }}/config/custom_templates/"
    mode: "0664"
  register: home_assistant_custom_templates
```

- [ ] **Step 4: Verify the role still renders cleanly**

Run: `cd /home/ubuntu/server && uv run ansible-playbook ansible/deploy.yml --tags "home-assistant" --skip-tags deploy --check`
Expected: no errors; the custom-templates copy task is evaluated. (`--check --skip-tags deploy` renders config without touching the container.)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml ansible/roles/containers/home-assistant/tasks/main.yml
git commit -m "$(printf 'test(home-assistant): wire Jinja macro suite into pytest + ship lighting.jinja\n\nAdd the HA tests dir to testpaths (runs in uv pytest, prek, CI) and switch\nthe custom_templates deploy to a directory copy so new macros ship.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 5: Rewire `bedroom_apply_fan` to the macro (behavior-preserving)

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (`bedroom_apply_fan`, the `variables:` block ~lines 234-263)

**Interfaces:**
- Consumes: `fan_target_level` (Task 2).
- Produces: identical `target_level` / `send_pct` behavior, now sourced from the macro.

- [ ] **Step 1: Add a migration-equivalence test (old inline vs macro)**

Append to `ansible/roles/containers/home-assistant/tests/test_fan_macros.py`:

```python
# Migration safety net: the extracted macro must equal the ORIGINAL inline bedroom_apply_fan
# formula for every input. This pins behavior-preservation of the Task 5 rewire; keep it as a
# permanent regression guard against the curve being changed in only one place.
def _inline_target(t, cur_level, is_night, sleep):
    # The pre-extraction formula, transcribed from scripts.yaml's bedroom_apply_fan.
    ideal = (t - 71) / 1.3 if t >= 0 else 0
    cap = 2 if sleep else (4 if is_night else 9)
    if t < 0 or ideal < 0.3:
        want = 0
    elif cur_level == 0 or ideal > cur_level + 0.7 or ideal < cur_level - 0.7:
        # banker's rounding, matching HA's forgiving_round
        from jinja_harness import _forgiving_round
        want = _forgiving_round(ideal)
    else:
        want = cur_level
    return min(want, cap)


def test_macro_matches_original_inline_formula():
    for t in [x / 10 for x in range(680, 900)]:        # 68.0 .. 89.9 °F
        for cur_level in range(0, 10):
            for is_night in (False, True):
                for sleep in (False, True):
                    assert _target(t, cur_level, is_night, sleep) == _inline_target(
                        t, cur_level, is_night, sleep
                    ), f"drift at t={t} cur={cur_level} night={is_night} sleep={sleep}"
```

- [ ] **Step 2: Run to verify it passes against the macro**

Run: `uv run pytest ansible/roles/containers/home-assistant/tests/test_fan_macros.py::test_macro_matches_original_inline_formula -v`
Expected: PASS — proves the macro is a faithful copy of the inline formula before we delete the inline version.

- [ ] **Step 3: Rewire the YAML**

In `ansible/roles/containers/home-assistant/files/scripts.yaml`, inside `bedroom_apply_fan`'s `variables:` block, REPLACE the `ideal`, `cap`, `is_night`, `sleep`, `want`, and `target_level` definitions with the following (keep `t`, `cur_pct`, `cur_level`, and `send_pct` exactly as they are). The `is_night`/`sleep` reads stay; the curve/hysteresis/cap math moves to the macro:

```yaml
        # Clock + sleep flag stay here (entity/time reads); the curve, hysteresis deadband, and the
        # night/sleep caps live in fan.jinja's fan_target_level (single source of truth, unit-tested
        # in tests/test_fan_macros.py). TUNE the curve there.
        is_night: "{{ now().hour >= 22 or now().hour < 6 }}"
        sleep: "{{ is_state('input_boolean.bedroom_sleep_mode', 'on') }}"
        target_level: "{% from 'fan.jinja' import fan_target_level %}{{ fan_target_level(t, cur_level, is_night, sleep) | int }}"
```

Leave `send_pct` (`{% from 'fan.jinja' import level_to_pct %}{{ level_to_pct(target_level) | int }}`) and everything below the `variables:` block unchanged.

- [ ] **Step 4: Verify YAML validity + macro tests still green**

Run: `cd /home/ubuntu/server && python3 -c "import yaml; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/scripts.yaml'))" && uv run pytest ansible/roles/containers/home-assistant/tests/test_fan_macros.py -q`
Expected: no YAML error; tests PASS.

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/scripts.yaml \
        ansible/roles/containers/home-assistant/tests/test_fan_macros.py
git commit -m "$(printf 'refactor(home-assistant): bedroom_apply_fan uses fan_target_level macro\n\nReplace the inline curve/hysteresis/cap math with the tested macro;\nmigration-equivalence test pins behavior preservation.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 6: Rewire `bedroom_apply_natural` (wake ramp + in_window) to macros

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/scripts.yaml` (`bedroom_apply_natural`, the nightlight + wake exceptions ~lines 81-115)

**Interfaces:**
- Consumes: `in_wake_window`, `wake_brightness`, `wake_transition` (Task 3).
- Produces: identical nightlight-condition, wake-condition, and brightness/transition behavior, sourced from macros.

- [ ] **Step 1: Rewire the nightlight exception condition**

In `bedroom_apply_natural`, REPLACE the nightlight exception's `value_template` (the `{% set ws ... %}{% set in_window ... %}{{ (is_state(...) or now().hour < 5) and not in_window }}` block) with:

```yaml
              value_template: >-
                {% set ws = states('sensor.bedroom_wake_start') %}
                {% from 'lighting.jinja' import in_wake_window %}
                {% set in_window = in_wake_window((now() - as_datetime(ws)).total_seconds() / 60 if ws not in ['unknown', 'unavailable'] else -1) | bool %}
                {{ (is_state('input_boolean.bedroom_sleep_mode', 'on') or now().hour < 5) and not in_window }}
```

- [ ] **Step 2: Rewire the wake exception condition**

REPLACE the wake exception's `value_template` (the `{% set ws ... %}{{ ws not in [...] and timedelta(0) <= ... < timedelta(minutes=15) }}` block) with:

```yaml
              value_template: >-
                {% set ws = states('sensor.bedroom_wake_start') %}
                {% from 'lighting.jinja' import in_wake_window %}
                {{ in_wake_window((now() - as_datetime(ws)).total_seconds() / 60 if ws not in ['unknown', 'unavailable'] else -1) | bool }}
```

- [ ] **Step 3: Rewire the wake brightness/transition**

In the wake exception's `sequence`, REPLACE the `variables:` block and the `bedroom_set_natural_brightness` data so the ramp math comes from the macros (drop the now-internal `wake_peak`):

```yaml
            - variables:
                # now()/sensor reads stay here; the ramp + sleep-aware peak live in lighting.jinja.
                wake_elapsed_min: "{{ (now() - as_datetime(states('sensor.bedroom_wake_start'))).total_seconds() / 60 }}"
                sleep_min: "{{ states('sensor.pixel_9_pro_sleep_duration') | float(0) }}"
            - service: script.bedroom_set_natural_brightness
              data:
                brightness_pct: "{% from 'lighting.jinja' import wake_brightness %}{{ wake_brightness(wake_elapsed_min, sleep_min) | int }}"
                transition: "{% from 'lighting.jinja' import wake_transition %}{{ wake_transition(wake_elapsed_min) | int }}"
```

- [ ] **Step 4: Verify YAML validity**

Run: `cd /home/ubuntu/server && python3 -c "import yaml; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/scripts.yaml'))" && echo OK`
Expected: `OK` (no YAML error).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/scripts.yaml
git commit -m "$(printf 'refactor(home-assistant): bedroom_apply_natural uses lighting macros\n\nWake-window conditions + ramp brightness/transition now come from\nlighting.jinja (in_wake_window/wake_brightness/wake_transition).\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 7: Rewire `bedroom_auto_light_allowed` (templates.yaml) to macros

**Files:**
- Modify: `ansible/roles/containers/home-assistant/files/templates.yaml` (`bedroom_auto_light_allowed` state, ~lines 33-36)

**Interfaces:**
- Consumes: `in_wake_window`, `auto_light_allowed` (Task 3).
- Produces: identical binary-sensor state, sourced from macros (the third and final inline `in_window` site collapses into the shared macro).

- [ ] **Step 1: Rewire the binary_sensor state**

In `ansible/roles/containers/home-assistant/files/templates.yaml`, REPLACE the `bedroom_auto_light_allowed` `state:` template with:

```yaml
      state: >-
        {% set ws = states('sensor.bedroom_wake_start') %}
        {% from 'lighting.jinja' import in_wake_window, auto_light_allowed %}
        {% set in_window = in_wake_window((now() - as_datetime(ws)).total_seconds() / 60 if ws not in ['unknown', 'unavailable'] else -1) | bool %}
        {{ auto_light_allowed(in_window, states('sensor.aqara_fp300_illuminance')) }}
```

- [ ] **Step 2: Verify YAML validity**

Run: `cd /home/ubuntu/server && python3 -c "import yaml; yaml.safe_load(open('ansible/roles/containers/home-assistant/files/templates.yaml'))" && echo OK`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/home-assistant/files/templates.yaml
git commit -m "$(printf 'refactor(home-assistant): auto_light_allowed sensor uses lighting macros\n\nCollapse the last inline in_window formula + the 50-lux threshold into\nlighting.jinja (in_wake_window/auto_light_allowed).\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 8: Full check, deploy, and verify live

**Files:** none (verification + deploy only)

- [ ] **Step 1: Run the whole repo test suite + prek**

Run: `cd /home/ubuntu/server && uv run pytest -q && prek run --all-files`
Expected: all tests PASS; prek hooks pass (yaml lint, ansible-lint, gitleaks, pytest, compose validation).

- [ ] **Step 2: Render-check the rewired config without writing it**

Run: `cd /home/ubuntu/server && uv run ansible-playbook ansible/deploy.yml --tags "home-assistant" --skip-tags deploy --check`
Expected: the config/templates/scripts/custom_templates copy tasks report `changed` (new macro logic); no errors. **Must be `--check`** — writing the files here (without `--check`) would make Step 3's copy tasks report no change, so `common_config_changed` would be false and the full deploy would NOT recreate the container, leaving the old macros loaded.

- [ ] **Step 3: Deploy (recreate the container, ~120s)**

Run: `cd /home/ubuntu/server && uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`
Expected: deploy completes; container recreated because the copied configs changed (`common_config_changed`).

- [ ] **Step 4: Gate on container health**

Run: `cd /home/ubuntu/server && uv run python scripts/probe.py health home-assistant`
Expected: exit 0 (running + healthy). If it fails, check `docker logs home-assistant` for a template/Jinja load error (e.g. a macro import path typo) before proceeding.

- [ ] **Step 5: Verify the macros render correctly live**

In HA → Developer Tools → Template, paste and confirm sane output (no traceback, values match expectations):

```jinja
{% from 'fan.jinja' import fan_target_level %}
target_level @ 78°F, cur 5, day, awake: {{ fan_target_level(78.02, 5, false, false) }}
{% from 'lighting.jinja' import wake_brightness, auto_light_allowed %}
wake_brightness @ elapsed 15, normal night: {{ wake_brightness(15, 0) }}
auto_light_allowed (not in window, 40 lux): {{ auto_light_allowed(false, 40) }}
```

Expected: `5`, `50`, `True`. Also confirm `binary_sensor.bedroom_auto_light_allowed` and `fan.tower_fan` hold sensible states (Developer Tools → States). Spot-check that the fan responds to the current temperature as before.

- [ ] **Step 6: Update role CLAUDE.md with the new testing layer**

Add a short note to `ansible/roles/containers/home-assistant/CLAUDE.md` under "Editing" (or a new "Testing" bullet): the bedroom Jinja math now lives in `custom_templates/{fan,lighting}.jinja` macros, unit-tested in `tests/` via `uv run pytest`; the harness mirrors HA's `forgiving_round` (banker's). Commit:

```bash
git add ansible/roles/containers/home-assistant/CLAUDE.md
git commit -m "$(printf 'docs(home-assistant): note the Jinja macro unit-test layer\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Self-Review Notes

- **Spec coverage:** harness + round pin (Task 1), fan macro (Task 2), lighting macros (Task 3), wiring + dir-copy (Task 4), the three caller rewires (Tasks 5-7), deploy & verify live (Task 8) — every spec section maps to a task.
- **Deferred items** (config validation, notify/threshold macros) are intentionally out of this plan per the spec.
- **Type consistency:** `render_macro(file, macro, *args)` and the `fan_target_level` / `in_wake_window` / `wake_brightness` / `wake_transition` / `auto_light_allowed` signatures are identical across the harness, the macro definitions, the tests, and the YAML call sites.
