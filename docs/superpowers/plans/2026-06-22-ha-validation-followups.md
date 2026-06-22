# HA Validation-Layer Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two deterministic checks to the HA validation layer — a symmetric single-writer check (catches a stale `sanctioned_writers.yml` entry) and a file-driven post-deploy assertion that every git-defined automation loaded.

**Architecture:** Two independent components. (A) extends one existing pure function in `scripts/ha_state_model.py` (already wired into the `validate-ha-config` prek hook). (B) adds two pure functions plus a read-only `ha verify-automations` subcommand to `scripts/probe.py`, wired into the `ha-deploy` skill as a post-deploy gate. All logic is pure functions with truth-table tests; the I/O shells are thin.

**Tech Stack:** Python 3 (stdlib only — no new deps), `uv run pytest`, prek hooks, the existing curl-based read-only `probe.py` HA client.

## Global Constraints

- **Deterministic, exit-code gated, no flakiness** — every check is a pure function over ground truth; tests carry no live dependency.
- **No heavy CI in GitHub.** Component A runs in the *existing* `validate-ha-config` prek hook (local + CI, already there — no new CI). Component B is **local-only** (post-deploy, needs the live HA + SOPS token).
- **Component A must stay GREEN on the real role** — `uv run python scripts/ha_state_model.py check` must remain clean (every current `module`/`exemptions` entry is already a live writer, so this is pure future-tightening with zero current breakage).
- **Component B matches git `id:` ↔ live `attributes.id`** (every live automation carries `attributes.id`; this sidesteps the alias-slug≠id trap — never derive a slug).
- **Component B is file-driven** — it checks only ids present in `automations.yaml`, so live `.storage`/UI cruft can never make its gate fail.
- **`probe.py` stays read-only** — `verify-automations` issues only `GET /api/states`. Do not add any write/WS call to probe.py.
- **`containers/` is read-only** — edit Ansible role sources only. (The CLAUDE.md touched here is the role's own doc under `ansible/`, which is fine.)
- A disabled automation (live `state == "off"`) is NOT an error — only a missing id or `state == "unavailable"` is.

---

### Task 1: Component A — symmetric single-writer check

**Files:**
- Modify: `scripts/ha_state_model.py` — `single_writer_errors` (currently at `:555`)
- Test: `scripts/test_ha_state_model.py`

**Interfaces:**
- Consumes: nothing new. `single_writer_errors(writes: dict, sanctioned: dict) -> list[str]` already exists and is already called by `check_errors`.
- Produces: same signature; the function now also returns "no longer writes it" errors for stale entries. No caller changes.

**Context:** The sibling `override_writer_errors` (`:463`) already checks both directions; this makes `single_writer_errors` match it. The current one-directional version only flags `derived − allowed`.

- [ ] **Step 1: Write the failing tests**

Add to `scripts/test_ha_state_model.py`:

```python
def test_single_writer_flags_stale_sanctioned_entry():
    # A sanctioned entry (module or exemption) that no longer writes the actuator is stale.
    writes = {"light.x": ["script.live_writer"]}
    sanctioned = {"light.x": {"module": ["script.live_writer"],
                              "exemptions": ["script.dead_writer"]}}
    errs = single_writer_errors(writes, sanctioned)
    assert errs == ["light.x: sanctioned writer script.dead_writer no longer writes it — "
                    "remove it from state/sanctioned_writers.yml"]


def test_single_writer_clean_when_derived_equals_allowed():
    writes = {"light.x": ["script.a", "script.b"]}
    sanctioned = {"light.x": {"module": ["script.a"], "exemptions": ["script.b"]}}
    assert single_writer_errors(writes, sanctioned) == []


def test_single_writer_still_flags_unsanctioned_writer():
    writes = {"light.x": ["script.a", "script.rogue"]}
    sanctioned = {"light.x": {"module": ["script.a"], "exemptions": []}}
    errs = single_writer_errors(writes, sanctioned)
    assert len(errs) == 1 and "unsanctioned writer script.rogue" in errs[0]
```

If `single_writer_errors` is not already imported at the top of the test file, add it to the existing `from ha_state_model import (...)` import group.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest scripts/test_ha_state_model.py -k single_writer -v`
Expected: `test_single_writer_flags_stale_sanctioned_entry` and `test_single_writer_clean_when_derived_equals_allowed` FAIL (the stale-entry error is not produced yet); `test_single_writer_still_flags_unsanctioned_writer` PASSES.

- [ ] **Step 3: Make the check symmetric**

Replace the body of `single_writer_errors` in `scripts/ha_state_model.py` with:

```python
def single_writer_errors(writes: dict, sanctioned: dict) -> list[str]:
    """HARD + symmetric: the derived writer set of each sanctioned actuator must equal
    module ∪ exemptions. An unsanctioned writer fails; a sanctioned entry that no longer
    writes the actuator fails too (a stale entry silently widens the allowed set — remove it).
    Mirrors override_writer_errors."""
    errs = []
    for actuator, spec in sorted(sanctioned.items()):
        allowed = set(spec.get("module", [])) | set(spec.get("exemptions", []))
        got = set(writes.get(actuator, []))
        for writer in sorted(got - allowed):
            errs.append(f"{actuator}: unsanctioned writer {writer} — route it through the mediator "
                        f"(script.bedroom_lights_set / bedroom_fan_set) or declare it in "
                        f"state/sanctioned_writers.yml")
        for stale in sorted(allowed - got):
            errs.append(f"{actuator}: sanctioned writer {stale} no longer writes it — "
                        f"remove it from state/sanctioned_writers.yml")
    return errs
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest scripts/test_ha_state_model.py -k single_writer -v`
Expected: all three PASS.

- [ ] **Step 5: Verify the real role stays green (no current breakage)**

Run: `uv run python scripts/ha_state_model.py check`
Expected: exit 0, no output about stale sanctioned writers (every current `module`/`exemptions` entry is a live writer in `derived_state.yml`).

- [ ] **Step 6: Commit**

```bash
git add scripts/ha_state_model.py scripts/test_ha_state_model.py
git commit -m "feat(ha-state): make single_writer_errors symmetric (catch stale sanctioned entries)"
```

---

### Task 2: Component B core — pure id-extraction + load-comparison functions

**Files:**
- Modify: `scripts/probe.py` — add `import re`, the `AUTOMATIONS_YAML` constant, and two pure functions
- Test: `scripts/test_probe.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `expected_automation_ids(text: str) -> set[str]` — the `id:` of every top-level automation in `automations.yaml` text.
  - `automation_load_errors(expected_ids, live_automations: list[dict]) -> list[str]` — error strings for defined-but-not-loaded and loaded-but-unavailable automations.

- [ ] **Step 1: Write the failing tests**

Add to `scripts/test_probe.py`:

```python
def test_expected_automation_ids_matches_top_level_only():
    from probe import expected_automation_ids
    text = (
        "- id: bedroom_presence_on\n"
        "  alias: Presence on\n"
        "  trigger:\n"
        "    - id: co2_bad\n"          # indented trigger id must NOT be captured
        "      platform: state\n"
        "- id: ha_heartbeat\n"
        "  alias: HA heartbeat\n"
    )
    assert expected_automation_ids(text) == {"bedroom_presence_on", "ha_heartbeat"}


def test_automation_load_errors_flags_missing_and_unavailable():
    from probe import automation_load_errors
    expected = {"a_loaded", "b_missing", "c_unavailable", "d_disabled"}
    live = [
        {"entity_id": "automation.a", "state": "on", "attributes": {"id": "a_loaded"}},
        {"entity_id": "automation.c", "state": "unavailable", "attributes": {"id": "c_unavailable"}},
        {"entity_id": "automation.d", "state": "off", "attributes": {"id": "d_disabled"}},
        {"entity_id": "automation.x", "state": "on", "attributes": {"id": "cruft_not_in_file"}},
    ]
    errs = automation_load_errors(expected, live)
    assert errs == [
        "automation b_missing is defined in automations.yaml but did not load",
        "automation c_unavailable loaded but is unavailable (config error at load)",
    ]


def test_automation_load_errors_clean_when_all_loaded():
    from probe import automation_load_errors
    expected = {"a", "b"}
    live = [
        {"entity_id": "automation.a", "state": "on", "attributes": {"id": "a"}},
        {"entity_id": "automation.b", "state": "off", "attributes": {"id": "b"}},
    ]
    assert automation_load_errors(expected, live) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest scripts/test_probe.py -k "automation_ids or automation_load" -v`
Expected: FAIL with `ImportError`/`AttributeError` (functions not defined yet).

- [ ] **Step 3: Add the constant, import, and pure functions**

In `scripts/probe.py`, add `import re` to the top-level imports (next to the existing `import os`).

Add this constant next to `HA_CONTAINER = "home-assistant"` (around `:44`):

```python
# Git-managed automation source (repo-root relative to this file) — the "expected" set for
# the verify-automations post-deploy gate. The deployed config is copied from here verbatim.
AUTOMATIONS_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ansible", "roles", "containers", "home-assistant", "files", "automations.yaml")

# Top-level automation list items only: `- id: <slug>` anchored at column 0. A trigger/condition
# `id:` is always indented, so it can never be mistaken for an automation id.
_AUTOMATION_ID_RE = re.compile(r"^- id:\s*(\S+)", re.MULTILINE)
```

Add the two pure functions (place them near `ha_trace`/`format_trace`, before `run_ha`):

```python
def expected_automation_ids(text: str) -> set[str]:
    """The `id:` of every top-level automation in automations.yaml text. Regex over the raw
    text (no YAML parse) — robust to the HA Jinja inside the file; ids are simple slugs."""
    return set(_AUTOMATION_ID_RE.findall(text))


def automation_load_errors(expected_ids, live_automations):
    """expected_ids = ids from automations.yaml; live_automations = the automation.* entries
    from /api/states. A defined id with no live automation carrying that attributes.id did NOT
    load (dropped). A defined id whose live automation is `unavailable` errored at load. A
    disabled automation (state 'off') is fine. Live ids not in the file (UI/.storage cruft) are
    ignored — this gate is file-driven so cruft can't make it red."""
    by_id = {}
    for a in live_automations:
        aid = (a.get("attributes") or {}).get("id")
        if aid is not None:
            by_id[aid] = a
    errs = []
    for aid in sorted(expected_ids):
        live = by_id.get(aid)
        if live is None:
            errs.append(f"automation {aid} is defined in automations.yaml but did not load")
        elif live.get("state") == "unavailable":
            errs.append(f"automation {aid} loaded but is unavailable (config error at load)")
    return errs
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest scripts/test_probe.py -k "automation_ids or automation_load" -v`
Expected: all three PASS.

- [ ] **Step 5: Sanity-check the regex against the real file**

Run: `uv run python -c "import sys; sys.path.insert(0,'scripts'); import probe; print(len(probe.expected_automation_ids(open(probe.AUTOMATIONS_YAML).read())))"`
Expected: `29`

- [ ] **Step 6: Commit**

```bash
git add scripts/probe.py scripts/test_probe.py
git commit -m "feat(probe): pure automation-id extraction + load-error comparison for verify-automations"
```

---

### Task 3: Component B shell — `ha verify-automations` subcommand + deploy integration

**Files:**
- Modify: `scripts/probe.py` — add the subparser entry (`_build_parser`, around `:399`) and the `run_ha` branch (around `:509`)
- Modify: `scripts/test_probe.py` — a parser-wiring test
- Modify: `.claude/skills/ha-deploy/SKILL.md` — add the gate to step 5
- Modify: `ansible/roles/containers/home-assistant/CLAUDE.md` — note the new subcommand in the probe.py bullet

**Interfaces:**
- Consumes: `expected_automation_ids`, `automation_load_errors` (Task 2); existing `resolve_ip`, `ha_get`, `ha_get_url`, `ha_token`, `ha_curl_argv`, `HA_CONTAINER` (probe.py).
- Produces: a `probe.py ha verify-automations` CLI command, exit 0 = all defined automations loaded.

- [ ] **Step 1: Write the failing parser-wiring test**

Add to `scripts/test_probe.py`:

```python
def test_verify_automations_subcommand_parses():
    from probe import _build_parser
    ns = _build_parser().parse_args(["ha", "verify-automations"])
    assert ns.cmd == "ha" and ns.ha_cmd == "verify-automations"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest scripts/test_probe.py -k verify_automations_subcommand -v`
Expected: FAIL (`SystemExit` from argparse — the subcommand isn't registered).

- [ ] **Step 3: Register the subparser**

In `_build_parser`, immediately after the `trace`/`why` subparser block (around `:402`), add:

```python
    hasub.add_parser("verify-automations",
                     help="assert every automation in automations.yaml loaded (exit 0 = all loaded)")
```

- [ ] **Step 4: Add the run_ha branch**

In `run_ha`, immediately after the `if ns.ha_cmd in ("trace", "why"):` block (which ends `return 0` around `:526`), add:

```python
    if ns.ha_cmd == "verify-automations":
        if ns.dry_run:
            print(" ".join(ha_curl_argv(ha_get_url("<ha-ip>", "states")))
                  + f"   # + Bearer; compare attributes.id against ids in {AUTOMATIONS_YAML}")
            return 0
        ip = resolve_ip(HA_CONTAINER)
        states = json.loads(ha_get(ha_get_url(ip, "states"), ha_token()))
        live = [s for s in states if s["entity_id"].startswith("automation.")]
        with open(AUTOMATIONS_YAML) as f:
            expected = expected_automation_ids(f.read())
        errs = automation_load_errors(expected, live)
        if errs:
            for e in errs:
                print(e)
            return 1
        print(f"all {len(expected)} automations loaded")
        return 0
```

- [ ] **Step 5: Run the parser test + full probe suite**

Run: `uv run pytest scripts/test_probe.py -v`
Expected: all PASS (including `test_verify_automations_subcommand_parses`).

- [ ] **Step 6: Verify live on daniel-server (the deliverable check)**

Run: `uv run python scripts/probe.py ha verify-automations`
Expected: `all 29 automations loaded`, exit 0. (The live instance was cleaned to match git — 29 automations, none unavailable.)

Also confirm the dry-run path: `uv run python scripts/probe.py --dry-run ha verify-automations`
Expected: prints the curl command for `/api/states` plus the comparison note; exit 0.

- [ ] **Step 7: Wire it into the ha-deploy skill**

In `.claude/skills/ha-deploy/SKILL.md`, in step 5 ("Prove it loaded"), add a bullet:

```markdown
   - **Assert ALL automations loaded** (not just one): `uv run python scripts/probe.py ha
     verify-automations` — exit 0 = every automation in `files/automations.yaml` is present in
     the live instance and not `unavailable`. A non-zero exit lists the dropped/errored ids
     (a schema error HA silently skipped at load). File-driven, so live `.storage`/UI cruft is
     ignored.
```

- [ ] **Step 8: Note it in the role CLAUDE.md**

In `ansible/roles/containers/home-assistant/CLAUDE.md`, in the `scripts/probe.py ha` bullet (the one listing `ha state` / `ha automation` / `ha get` / `ha why`), append:

```markdown
 · `ha verify-automations` (post-deploy gate: exit 0 = every automation in files/automations.yaml
 loaded + not unavailable; matches git id ↔ live attributes.id; file-driven so .storage/UI cruft
 is ignored).
```

- [ ] **Step 9: Run the full repo suite + prek to confirm nothing regressed**

Run: `uv run pytest scripts -q`
Expected: all PASS.

Run: `prek run --all-files`
Expected: all hooks pass.

- [ ] **Step 10: Commit**

```bash
git add scripts/probe.py scripts/test_probe.py .claude/skills/ha-deploy/SKILL.md \
        ansible/roles/containers/home-assistant/CLAUDE.md
git commit -m "feat(probe): ha verify-automations post-deploy gate + wire into ha-deploy"
```

---

## Notes for the executor

- **No live deploy is required by this plan.** Component A changes only validation code; Component B adds a read-only diagnostic. The live HA is already in the clean state the gate expects (29 automations, none unavailable).
- **Subagent commit caveat (from the predecessor run):** if executed via subagent-driven-development, implementer subagents report only — the controller runs the gate and commits explicit paths.
- **Task ordering:** Task 3 depends on Task 2 (`expected_automation_ids` / `automation_load_errors` must exist). Task 1 is independent and may go first or last.
