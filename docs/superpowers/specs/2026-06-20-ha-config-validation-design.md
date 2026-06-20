# Home Assistant — lightweight config validation (prek)

**Date:** 2026-06-20
**Status:** Approved design, pending implementation plan

## Problem

The bedroom Home Assistant config is split across an Ansible-templated `configuration.yaml` and
several `!include`d static files (`automations.yaml`, `scripts.yaml`, `templates.yaml`,
`scenes.yaml`, `customize.yaml`) plus shared `custom_templates/*.jinja` macros. Today nothing
validates these *before* a deploy: a YAML syntax error, a duplicate mapping key, a broken
`!include`, or a malformed inline Jinja template is only discovered when the container recreates
(~120 s) and either logs a config error or comes up unhealthy.

The new Jinja **unit** tests (2026-06-19) cover the *extracted macros'* computed behavior. They do
NOT cover the structural integrity of the config files or the syntax of the large body of
**inline** automation Jinja. This spec adds a fast, pre-deploy structural gate.

## Goal & scope

A pure-Python validator (`PyYAML` + `Jinja2`, both already dev deps — **no Docker, no Home
Assistant dependency**) wired as a prek hook so it runs locally on commit and in CI. It catches:

1. **YAML syntax errors** in any config file.
2. **Duplicate mapping keys** (HA's loader rejects these; stock PyYAML silently keeps the last).
3. **Broken `!include` targets** (a referenced file that doesn't exist).
4. **Malformed inline Jinja** — unbalanced `{% %}` / bad template syntax in any `{{ }}`/`{% %}`
   string, plus the `custom_templates/*.jinja` files.

**Out of scope (YAGNI):** HA *schema* validation (unknown keys, bad integration options) and
entity-reference checks. Those require the real `hass --script check_config` in a Docker HA image
(the heavier option, explicitly not chosen). The deploy still catches schema errors live.

## Key facts that shape the design

- **No config file uses `!secret` or Ansible `{{ }}`/`{% %}` templating.** All five `templates/*.j2`
  files render verbatim (the `.j2` extension is vestigial), so "assembling the config" is a plain
  file copy — no Ansible render engine and no SOPS access needed. The validator asserts this
  (fails loudly if a `templates/*.j2` ever gains real Ansible markers, which would violate the
  repo's copy-not-template rule for HA files and require a real render).
- **HA Jinja never breaks YAML parsing** — it always lives inside quoted strings or `>-` block
  scalars, so a YAML loader reads it as plain string content. This is what lets a pure-Python
  loader validate the whole tree without rendering any Jinja.
- **Only `!include` and (potentially) `!secret`/`!env_var` HA tags appear**; the config uses just
  `!include` today. Jinja `Environment().parse()` is purely syntactic — it needs no filters,
  globals, or state, and does not resolve `{% from 'fan.jinja' import %}` — so it can syntax-check
  every template string standalone.

## Architecture

Three components, mirroring the existing `scripts/validate_compose_templates.py` +
`validate-compose-templates` prek hook pattern.

### 1. `scripts/validate_ha_config.py`

- **`HAConfigLoader(yaml.SafeLoader)`** — a SafeLoader subclass that adds HA semantics:
  - `!include <path>` constructor → resolve relative to the *current file's* directory and
    recursively load it (so the entire tree loads in one pass; a missing target raises a clear
    error → broken-include caught).
  - `!secret` / `!env_var` constructors → return a placeholder string (values aren't validated).
  - Override `construct_mapping` to **detect duplicate keys** and raise with the offending
    `file:line` (via the node's `start_mark`).
- **`assemble_config(role_dir) -> Path`** — copy the deployed `/config` layout into a temp dir:
  `templates/{configuration,customize,ui-lovelace}.yaml.j2` → `*.yaml`, plus all of `files/*`
  (`automations.yaml`, `scenes.yaml`, `scripts.yaml`, `templates.yaml`, `custom_templates/`).
  Raise if any copied `templates/*.j2` contains `{{`/`{%` Ansible markers (guard described above).
- **`structural_errors(config_path) -> list[str]`** — load `configuration.yaml` through
  `HAConfigLoader`; return any YAML / duplicate-key / broken-include errors. The recursive
  `!include` means one load transitively validates every included file.
- **`jinja_errors(loaded, custom_templates_dir) -> list[str]`** — walk the loaded structure; for
  every string value containing `{{` or `{%`, call `Environment().parse(value)` and collect
  `TemplateSyntaxError`s with a path/snippet. Also `parse()` each `custom_templates/*.jinja` file.
- **`validate(role_dir) -> list[str]`** — orchestrates assemble → structural → jinja; returns the
  combined error list.
- **CLI `main()`** — run `validate()` against the home-assistant role; print errors; exit 1 if any,
  else 0. Runnable directly or via the prek hook.

### 2. prek hook (`prek.toml`)

A `local`/`system` hook mirroring `validate-compose-templates`:

```toml
[[repos.hooks]]
id = "validate-ha-config"
name = "Validate Home Assistant config"
entry = "uv run python scripts/validate_ha_config.py"
language = "system"
pass_filenames = false
files = "^(ansible/roles/containers/home-assistant/(templates|files)/.*|scripts/validate_ha_config\\.py)$"
```

Runs on any change under the role's `templates/`+`files/`, locally and in CI's `prek run
--all-files`. (The existing `pytest` hook's `files` regex already covers `scripts/.*\.py`, so the
validator's own tests run there automatically.)

### 3. Tests — `scripts/test_validate_ha_config.py`

The `scripts` dir is already in `pyproject.toml` `testpaths`. TDD coverage:

- The **real role config validates clean** (`validate()` returns `[]`) — a regression guard on the
  live config.
- **Duplicate mapping key** in a temp fixture → reported.
- **Missing `!include` target** → reported.
- **Malformed YAML** (bad indentation) → reported.
- **Unclosed `{% if %}`** in a template string and in a `custom_templates/*.jinja` → reported.
- **Ansible-marker guard**: a `templates/*.j2` fixture containing `{{ ansible_var }}` → reported.

## Testing strategy

TDD per repo norm: write each failing test against a small fixture first, then implement the
matching validator capability. The "real role config passes" test pins behavior against the actual
deployed config and runs in the `scripts` pytest suite + the prek `pytest` hook + CI.

## Risks & mitigations

- **False positives from the Jinja parse-check** — mitigated by using `parse()` (syntax only, no
  filter/global resolution), which accepts HA's custom filters and `from/import` syntax. If a
  legitimate construct ever trips it, narrow the walk or whitelist, don't disable.
- **Validator drifts from the deployed layout** — `assemble_config` mirrors `tasks/main.yml`'s copy
  set; the "real config passes" test fails if the assembly misses a file. Keep the two in sync.
- **A future `!secret`/Ansible var** — the loader handles `!secret` as a placeholder; the
  Ansible-marker guard fails loudly if real templating is added to an HA config file.
