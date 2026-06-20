# Tap Dial 4-Button Redesign — Implementation Report

**Date:** 2026-06-19
**Tasks executed:** 1, 2, 3 (Task 4 = deploy/verify, left for human)

---

## Files Changed

| File | Change |
|------|--------|
| `ansible/roles/containers/home-assistant/files/scenes.yaml` | Appended `bedroom_relax` scene (warm 2200 K, 30%) |
| `ansible/roles/containers/home-assistant/files/scripts.yaml` | Inserted `bedroom_apply_natural_gated` script after `bedroom_apply_natural` |
| `ansible/roles/containers/home-assistant/files/automations.yaml` | Replaced `action:` block of `bedroom_tap_dial_control` (B1–B4 + dial) |
| `ansible/roles/containers/home-assistant/CLAUDE.md` | Updated Tap Dial parenthetical description |
| `ansible/roles/containers/home-assistant/SETUP.md` | Replaced bullet list with press/hold table |

---

## Three Commit Short-Hashes

```
609d149  home-assistant: add bedroom_relax scene + lux-gated natural wrapper
e907a03  home-assistant: re-map Tap Dial to Power/Brightness/Sleep/Fan (no overlap)
286b775  docs(home-assistant): update Tap Dial button map to the new 4-button layout
```

---

## YAML Validation Output

Command run after all edits:

```bash
uv run python -c "
import yaml
yaml.safe_load(open('ansible/roles/containers/home-assistant/files/scenes.yaml'))
print('scenes.yaml: OK')
yaml.safe_load(open('ansible/roles/containers/home-assistant/files/scripts.yaml'))
print('scripts.yaml: OK')
yaml.safe_load(open('ansible/roles/containers/home-assistant/files/automations.yaml'))
print('automations.yaml: OK')
"
```

Output:
```
scenes.yaml: OK
scripts.yaml: OK
automations.yaml: OK
```

Per-task validation (as required by the plan) also printed `OK` before each commit — no indentation or quoting errors were encountered.

---

## bedroom_apply_natural and bedroom_presence_on — Untouched Confirmation

- `bedroom_apply_natural` (the script): only lines added were the new `bedroom_apply_natural_gated` block inserted after it. The `bedroom_apply_natural:` function body itself was not modified (confirmed via `git show 609d149 -- files/scripts.yaml | grep "^[-+]"`).
- `bedroom_presence_on` (the automation): the `bedroom_tap_dial_control` action block replacement did not touch any other automation. `grep -n "bedroom_presence_on" automations.yaml` shows it at line 137 exactly as before (its `id`, `alias`, `description`, triggers, conditions, and `action: - service: script.bedroom_apply_natural` are all unmodified). The diff for `e907a03` shows 0 lines changed outside the `bedroom_tap_dial_control` action block.

---

## Pre-commit Hook Results

All three commits passed all hooks:
- trim trailing whitespace: Passed
- fix end of files: Passed
- check yaml: Passed (or Skipped for non-YAML commits)
- ansible-lint: Passed
- Detect hardcoded secrets: Passed

---

## Concerns / Notes

None. The implementation matched the plan exactly:

1. **Task 1** — `scene.bedroom_relax` appended to scenes.yaml; `bedroom_apply_natural_gated` inserted into scripts.yaml after the `bedroom_apply_natural` default block, before the `# Air-quality alert pulse` comment. YAML validated OK before commit.

2. **Task 2** — The `action:` block of `bedroom_tap_dial_control` was replaced wholesale. The lines above `action:` (id, alias, description, mode, max, trigger, condition) were left exactly as they were. New layout: B1 press = smart toggle (on → `bedroom_apply_natural` ungated, off → off + manual-off); B1 hold = reset-to-auto (lux-gated via `bedroom_apply_natural_gated` + fan); B2 press = `scene.bedroom_relax`, hold = `scene.bedroom_bright`; B3 press = `scene.bedroom_nightlight`, hold = `script.bedroom_bedtime`; B4 press = fan auto, hold = boost 100%; dial = ±12% brightness (unchanged). YAML validated OK before commit.

3. **Task 3** — CLAUDE.md parenthetical replaced verbatim from the plan. SETUP.md bullet list replaced with the press/hold table from the plan.

**Task 4 (deploy + verify) was NOT executed** per the instruction — a human must run `uv run ansible-playbook ansible/deploy.yml --tags home-assistant` and verify all eight button actions.
