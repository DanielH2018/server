---
id: "0028"
title: The cluster render supports a closed Jinja subset
status: Proposed
date: 2026-11-06
governs:
  - "scripts/renderer/renderer_lib/dialect.py#a closed list, read by the refusal and the census"
---

# ADR-0028: The cluster render supports a closed Jinja subset

## Status

Proposed. The operator approves it, which sets the status to Accepted.

## Context

`homelab-render` rebuilds Ansible's variable precedence, `hostvars` and `include_role` vars, and
it reuses ansible-core's filters. R4.7 deletes the k8s play, and with it the Ansible render that
`shadow_diff.py compare` reads. R4.8 reimplements the filters, and H7 removes ansible-core, which
removes the filters' parity oracle. Nothing but the code would then define the Jinja the renderer accepts,
and a change to that code could move a rendered byte with no check to say which behaviour was
intended.

## Decision

The cluster render accepts the constructs and the variable semantics below, and nothing else.
`scripts/renderer/renderer_lib/dialect.py` holds the list. `renderer_env` refuses any other
filter, test or lookup with an error that names it, and a census holds every template and variable
value of the k8s roles to the list. Each construct has a fixture and an expected output as a
literal. The PR that added them checked each literal once against ansible-core, so the literals
outlive H7.

### Filters

| Filter | Source | Note |
|---|---|---|
| `authelia_service_rules` | `ansible/filter_plugins/` | See the spec case. |
| `basename` | ansible-core | See the spec case. |
| `bool` | ansible-core | `'false'`, `'no'`, `'0'` and `0` read False. Not Python's `bool()`. |
| `capitalize` | Jinja | See the spec case. |
| `combine` | ansible-core | See the spec case. |
| `default` | Jinja | `default(x, true)` also replaces a falsy value. |
| `dictsort` | Jinja | See the spec case. |
| `entry_names` | `ansible/filter_plugins/` | See the spec case. |
| `entry_port` | `ansible/filter_plugins/` | See the spec case. |
| `first` | Jinja | See the spec case. |
| `flux_components` | `ansible/filter_plugins/` | See the spec case. |
| `format` | Jinja | See the spec case. |
| `from_yaml` | ansible-core | See the spec case. |
| `grafana_deploy_annotated` | `ansible/filter_plugins/` | See the spec case. |
| `hash` | ansible-core | Takes the algorithm's name, such as `sha256`. |
| `homelab_role_label` | `ansible/filter_plugins/` | Re-serialises the text with `homelab/role` on every document. |
| `homepage_href` | `ansible/filter_plugins/` | See the spec case. |
| `homepage_widget_url` | `ansible/filter_plugins/` | See the spec case. |
| `in_service_tier` | `ansible/filter_plugins/` | See the spec case. |
| `indent` | Jinja | See the spec case. |
| `ingress_probe_targets` | `ansible/filter_plugins/` | See the spec case. |
| `int` | Jinja | See the spec case. |
| `join` | Jinja | See the spec case. |
| `lan_hostnames` | `ansible/filter_plugins/` | See the spec case. |
| `length` | Jinja | See the spec case. |
| `list` | Jinja | See the spec case. |
| `longhorn_backup_name` | `ansible/filter_plugins/` | See the spec case. |
| `longhorn_nobackup_claims` | `ansible/filter_plugins/` | See the spec case. |
| `longhorn_weekly_claims` | `ansible/filter_plugins/` | See the spec case. |
| `lower` | Jinja | See the spec case. |
| `map` | Jinja | See the spec case. |
| `max` | Jinja | See the spec case. |
| `metrics_port` | `ansible/filter_plugins/` | See the spec case. |
| `min` | Jinja | See the spec case. |
| `netpol_caller_fences` | `ansible/filter_plugins/` | See the spec case. |
| `netpol_callers` | `ansible/filter_plugins/` | See the spec case. |
| `published_ports` | `ansible/filter_plugins/` | See the spec case. |
| `reject` | Jinja | See the spec case. |
| `replace` | Jinja | See the spec case. |
| `reverse` | Jinja | See the spec case. |
| `scrape_jobs` | `ansible/filter_plugins/` | See the spec case. |
| `selectattr` | Jinja | See the spec case. |
| `sort` | Jinja | See the spec case. |
| `string` | Jinja | See the spec case. |
| `tier_backup_claims` | `ansible/filter_plugins/` | See the spec case. |
| `tier_priority_class` | `ansible/filter_plugins/` | See the spec case. |
| `to_json` | ansible-core | Keeps key order and escapes non-ASCII to `\uXXXX`. No `<` escape, unlike `tojson`. |
| `tojson` | Jinja | Sorts keys and escapes `<`, `>`, `&` and `'` for HTML. |
| `trim` | Jinja | See the spec case. |
| `unique` | Jinja | See the spec case. |
| `updater_lag_grace_s` | `ansible/filter_plugins/` | See the spec case. |
| `upper` | Jinja | See the spec case. |
| `urlencode` | Jinja | See the spec case. |
| `weekly_backup_group` | `ansible/filter_plugins/` | See the spec case. |

### Tests

A test named as an argument of `select`, `reject`, `selectattr` or `rejectattr` counts as a use,
as a filter named as an argument of `map` does.

| Test | Note |
|---|---|
| `defined` | Jinja. True when a layer supplies the name. |
| `equalto` | Jinja. Used by name in `selectattr`. |
| `in` | Jinja. Used by name in `selectattr`. |
| `none` | Jinja. |
| `search` | ansible-core. A regex search anywhere in the value. |

### Lookups

| Lookup | Note |
|---|---|
| `file` | Reads a path. Strips trailing whitespace unless `rstrip=False`. |
| `pipe` | Only `base64 -w0 <path>`, computed without a shell. Anything else fails the render. |
| `template` | Renders a path with the caller's variables, keeping its trailing newlines. `ansible.builtin.template` is the same lookup. |
| `vars` | Reads a variable named at render time. |

### Variable semantics

The layers, weakest first:

1. Role defaults.
1. `group_vars/all.yml`.
1. The host's `host_vars` file.
1. The role's `k8s/manifests` include vars, for the five keys `cluster.py:INCLUDE_KEYS` names. No
   other include var reaches a template.
1. The stub file's or placeholder mode's values, as `-e @FILE` extra vars do.

A value resolves after the layers merge, so a default that names a variable reads the winning
layer's value.

`hostvars` holds every host in `hosts.ini`. The rendered host's entry is the rendered context.
Another host's entry holds its `hosts.ini` inline variables under its own `host_vars` file, each
resolved against that host's variables.

An undefined variable fails the render and the error names it. `default` is the way to read a
name that may be absent.

### Where the subset differs from Ansible

Each of these fails the render or changes nothing the tree holds, so a template cannot rely on the
Ansible behaviour.

- `hostvars[<another host>]` holds no `group_vars` key. Ansible's holds them, and reading one
  fails the render.
- An include var outside `INCLUDE_KEYS` does not reach a template. Ansible's does.
- A `hosts.ini` inline value keeps its quotes. Ansible's parser strips them. `hosts.ini` quotes no
  value.
- `lookup('pipe')` runs nothing. It accepts one command, and Ansible runs any command.

## Consequences

A template that needs a new filter costs a PR with three edits: the literal, this table and a spec
case. That cost is the intended one, because R4.8 has to reimplement each ansible-core entry.

The refusal binds `renderer_env`, which the cluster render and its task-computed values use.
`strict_env`, which host and package modes render through, has every registered filter, and
`resolve_vars` resolves variable values in its own environment, so the render refuses none of
them. The census covers them instead: role defaults, inventory values, and the values of each
role's `k8s/manifests` include for the keys `cluster.py:INCLUDE_KEYS` and `restart.py:TARGET_KEYS`
name. H3 owns the host templates' subset.

The list holds filters no template names, because the expressions in role tasks and defaults use
them: `combine` and `map` for observability's rollouts, and `reject` and the Longhorn claim filters
in defaults. R4.3 moves the task expressions into `renderer_lib`, and a later PR may drop what
nothing then reads.

The removed filters are those the tree stopped using before this record, such as `to_uuid`, and
`register_ansible_filters` still registers them for the render guards. R4.8 reimplements only what
this list names.

## Governs

- `scripts/renderer/renderer_lib/dialect.py#a closed list, read by the refusal and the census`:
  the literal sets.
- `scripts/renderer/tests/test_renderer_dialect.py` holds the literals, the census and this
  record's tables.
