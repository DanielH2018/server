# HA Macro-Output `| bool` Coercion Lint (Component B) — Design

**Date:** 2026-06-22
**Status:** approved (pre-plan)
**Origin:** Component B from the validation work — deferred earlier (memory `ha-deferred-followups`),
now picked up. A `custom_templates` macro renders a **string** (`"True"`/`"False"`), which is always
truthy; consuming it raw in a boolean operator silently ignores the macro's verdict. This bit
`automation.ha_runtime_error_alert`'s condition (`error_in_scope(...) and …` → the gate was ignored;
fixed with `| bool`, commit `4f67071b`). This converts the `| bool` convention into a deterministic
pre-deploy gate.

## Governing principle

> Deterministic, AST-based (not regex), no flaky checks. The parser already distinguishes the bug
> ("macro string fed to a boolean operator") from the legitimate patterns ("macro string compared to
> a token", "macro string rendered standalone"), so the check is precise by construction. Rides the
> existing `validate-ha-config` prek hook (local + CI); no new CI.

## Goal

Fail validation when a known `custom_templates` macro call is used as an `and` / `or` / `not`
operand without a `| bool` filter — the exact string-truthiness footgun.

## Non-Goals

- **Scope = boolean operators only** (`and`/`or`/`not` operands). NOT `{% if macro() %}` tests or
  ternary (`a if macro() else b`) tests — a fuzzier boundary, rarer for macro calls, more
  false-positive surface. (Operator-misuse is the failure that actually occurred.)
- **Only known custom_templates macros** are flagged. HA builtins (`states()`, `is_state()`, …) are
  never flagged — they may lean on string-truthiness intentionally and are not our concern.
- **A *bare* macro Call is the flag; any filter-wrapping is accepted.** The implementation flags a
  macro call used directly as a boolean operand; a call wrapped in any filter is a `nodes.Filter`
  (not a `Call`) and passes, because any coercion removes the string-truthiness this check targets.
  The canonical/expected fix is `| bool`; distinguishing it from `| int` is unnecessary precision
  (nobody coerces a boolean operand with `| int`).
- **No regex.** Macro names and operand shapes come from the Jinja AST.

## Components — all in `scripts/validate_ha_config.py`

`jinja_errors` already parses every inline template (from the loaded `trees`) and every
`custom_templates/*.jinja` file via `env.parse()` (the AST) and discards it. This check walks that
AST instead. Add `from jinja2 import nodes` to the imports.

### 1. Macro-name extraction (AST, not regex)

```python
def _macro_names(custom_templates_dir: Path, env: Environment) -> set[str]:
    names: set[str] = set()
    for jinja_file in sorted(custom_templates_dir.glob("*.jinja")):
        try:
            ast = env.parse(jinja_file.read_text())
        except TemplateSyntaxError:
            continue  # syntax errors are reported by jinja_errors; skip here
        names |= {m.name for m in ast.find_all(nodes.Macro)}
    return names
```

Using `nodes.Macro` avoids the prose-capture a regex hits (e.g. "macro argument" in a comment).

### 2. Pure detection helper (unit-testable)

```python
def uncoerced_macro_bool_uses(template: str, macro_names: set[str],
                              env: Environment | None = None) -> list[str]:
    """Names of known macros used as a bare and/or/not operand (no `| bool`) in `template`.
    A `| bool`-wrapped call is a nodes.Filter (not a Call) -> not flagged; a Compare (`== 'x'`)
    or standalone `{{ macro() }}` is not an and/or/not operand -> not flagged."""
    env = env or Environment()
    ast = env.parse(template)

    def bare_macro_call(node) -> str | None:
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
```

`find_all` recurses, so nested/chained boolean expressions and operands inside call-args are all
covered.

### 3. Integration wrapper (wired into `validate()`)

```python
def macro_bool_coercion_errors(trees: list, custom_templates_dir: Path) -> list[str]:
    env = Environment()
    macro_names = _macro_names(custom_templates_dir, env)
    if not macro_names:
        return []
    errs: list[str] = []
    sources = [t for tree in trees for t in _iter_template_strings(tree)]
    sources += [f.read_text() for f in sorted(custom_templates_dir.glob("*.jinja"))]
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

Called in `validate()` right after the `jinja_errors(...)` line:
`errors += macro_bool_coercion_errors(trees, dest / "custom_templates")`.

## Testing — `scripts/test_validate_ha_config.py`

Truth table over `uncoerced_macro_bool_uses(template, {"m", "n"})`:

| template | expect |
|---|---|
| `{{ m() and x }}` | `['m']` |
| `{{ x or m() }}` | `['m']` |
| `{{ not m() }}` | `['m']` |
| `{{ (m() \| bool) and x }}` | `[]` |
| `{{ m() == 'wake' }}` | `[]` |
| `{{ m() }}` | `[]` |
| `{{ states('x') and y }}` | `[]` (unknown name) |
| `{{ m() and n() }}` | `['m', 'n']` |

Plus a corpus guard: `macro_bool_coercion_errors` over the real assembled role returns `[]`
(confirmed safe — no current macro is a raw boolean operand).

## Current-config safety

Verified: the only historical boolean-operand macro use was `error_in_scope`, now `| bool`-coerced,
so the check is green on the real role today. Pure future-tightening; zero current breakage.

## Boundaries

Two pure functions (macro-name extraction, operand detection) + one integration wrapper + a one-line
wire — all in `validate_ha_config.py`, reusing its existing AST parsing. The only non-trivial logic
(the operand walk) is pure and unit-tested. Closes the deferred Component B; the
`ha-deferred-followups` memory's remaining item becomes just the Grafana dashboard.
