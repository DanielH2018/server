# HA Macro-Output `| bool` Coercion Lint (Component B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fail `validate-ha-config` when a known `custom_templates` macro is used as a bare `and`/`or`/`not` operand (no `| bool`) — the string-truthiness footgun.

**Architecture:** AST-based check in `scripts/validate_ha_config.py`, reusing the `env.parse()` machinery `jinja_errors` already uses. A pure operand-walk helper + a macro-name extractor + an integration wrapper wired into `validate()`. Runs in the existing `validate-ha-config` prek hook (local + CI).

**Tech Stack:** Python 3, `jinja2` AST (`jinja2.nodes`), `uv run pytest`.

## Global Constraints

- **AST, not regex** — macro names from `nodes.Macro`; operands from `nodes.And`/`Or`/`Not`.
- **Scope = boolean operators only** (`and`/`or`/`not` operands). NOT `if`/ternary tests.
- **Only known custom_templates macros** are flagged — HA builtins (`states()`, `is_state()`) never.
- **A *bare* macro `Call` is the flag.** A `| bool`-wrapped call is a `nodes.Filter` (not a `Call`) → passes; any filter-wrapping passes. `Compare` (`== 'x'`) and standalone `{{ macro() }}` aren't operands → pass.
- **Must stay GREEN on the real role** — `validate_ha_config.py` must return `[]` (no current macro is a raw boolean operand; pure future-tightening).
- No new dependencies (`jinja2` already used). No deploy (validation code only).

---

### Task 1: The AST lint + wiring + tests

**Files:**
- Modify: `scripts/validate_ha_config.py` — add `nodes` import + 3 functions; wire into `validate()`
- Test: `scripts/test_validate_ha_config.py`

**Interfaces:**
- Consumes: the loaded `trees` and `custom_templates` dir already available in `validate()`; the `Environment`/`TemplateSyntaxError`/`_iter_template_strings` already in the module.
- Produces: `uncoerced_macro_bool_uses(template, macro_names, env=None) -> list[str]`; `_macro_names(dir, env) -> set[str]`; `macro_bool_coercion_errors(trees, custom_templates_dir) -> list[str]`.

- [ ] **Step 1: Write the failing truth-table tests**

Add to `scripts/test_validate_ha_config.py` (import the function from `validate_ha_config`):

```python
def test_uncoerced_macro_bool_uses_truth_table():
    from validate_ha_config import uncoerced_macro_bool_uses as u
    names = {"m", "n"}
    assert u("{{ m() and x }}", names) == ["m"]
    assert u("{{ x or m() }}", names) == ["m"]
    assert u("{{ not m() }}", names) == ["m"]
    assert u("{{ (m() | bool) and x }}", names) == []
    assert u("{{ m() == 'wake' }}", names) == []
    assert u("{{ m() }}", names) == []
    assert u("{{ states('x') and y }}", names) == []   # unknown name, not a tracked macro
    assert u("{{ m() and n() }}", names) == ["m", "n"]  # both operands, sorted
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest scripts/test_validate_ha_config.py -k uncoerced_macro_bool -v`
Expected: FAIL — `ImportError: cannot import name 'uncoerced_macro_bool_uses'`.

- [ ] **Step 3: Add the `nodes` import**

In `scripts/validate_ha_config.py`, add to the jinja2 imports (next to the existing
`from jinja2 import Environment` / `TemplateSyntaxError`):

```python
from jinja2 import nodes
```

- [ ] **Step 4: Add the three functions**

Add to `scripts/validate_ha_config.py` (place them just before `validate()`):

```python
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


def uncoerced_macro_bool_uses(template: str, macro_names: set[str],
                              env: Environment | None = None) -> list[str]:
    """Sorted names of known macros used as a BARE and/or/not operand (no `| bool`) in `template`.
    A `| bool`-wrapped call is a nodes.Filter (not a Call) -> not flagged; a Compare (`== 'x'`) or a
    standalone `{{ macro() }}` is not an and/or/not operand -> not flagged. find_all recurses, so
    nested/chained boolean expressions and operands inside call-args are covered."""
    env = env or Environment()
    ast = env.parse(template)

    def bare_macro_call(node):
        if (isinstance(node, nodes.Call) and isinstance(node.node, nodes.Name)
                and node.node.name in macro_names):
            return node.node.name
        return None

    bad: list[str] = []
    for op in list(ast.find_all(nodes.And)) + list(ast.find_all(nodes.Or)):
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
                errs.append(f"macro {name}() used as a boolean and/or/not operand without "
                            f"`| bool` — a macro renders a STRING (always truthy), so coerce it: "
                            f"in: {snippet!r}")
        except TemplateSyntaxError:
            continue  # reported by jinja_errors
    return errs
```

- [ ] **Step 5: Run the truth-table tests to verify they pass**

Run: `uv run pytest scripts/test_validate_ha_config.py -k uncoerced_macro_bool -v`
Expected: PASS.

- [ ] **Step 6: Wire it into `validate()`**

In `validate()`, immediately after the existing `errors += jinja_errors(trees, dest / "custom_templates")` line, add:

```python
        errors += macro_bool_coercion_errors(trees, dest / "custom_templates")
```

- [ ] **Step 7: Write the corpus-green guard test**

Add to `scripts/test_validate_ha_config.py`:

```python
def test_macro_bool_coercion_clean_on_real_role():
    # The real role must pass — no current macro is a raw boolean operand (error_in_scope is
    # `| bool`-coerced). Pure future-tightening; this guards against a false-positive regression.
    import validate_ha_config
    errors = validate_ha_config.validate()
    assert all("boolean and/or/not operand" not in e for e in errors), errors
```

Asserts specifically that NO `macro …() used as a boolean …` error is present on the real role
(robust to any unrelated validator error; the real role is in fact fully clean today).

- [ ] **Step 8: Run the corpus guard + the real validator**

Run: `uv run pytest scripts/test_validate_ha_config.py -k "uncoerced_macro_bool or macro_bool_coercion" -v`
Expected: PASS.

Run: `uv run python scripts/validate_ha_config.py`
Expected: exit 0, no "boolean and/or/not operand" error.

- [ ] **Step 9: Run the full scripts suite**

Run: `uv run pytest scripts -q`
Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
git add scripts/validate_ha_config.py scripts/test_validate_ha_config.py
git commit -m "feat(ha-validation): lint macro output used as a bare and/or/not operand (require | bool)"
```

---

## Notes for the executor

- No deploy — validation code only (runs in the prek hook).
- After this lands, update memory `ha-deferred-followups` to drop Component B (Grafana dashboard remains). (Controller bookkeeping, not a code step.)
- If executed via subagent-driven-development: the implementer reports only; the controller runs the gate and commits explicit paths.
