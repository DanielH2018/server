#!/usr/bin/env python3
"""Integrity tests for the .claude/ setup wiring.

Catches the class of silent breakage where a reference drifts away from its target:
a hook command in settings.json points at a renamed script, a .sh wrapper invokes a
.py that moved, a skill tells Claude to run a probe.py subcommand that no longer
exists, or a skill/agent is missing the frontmatter it needs to load. None of these
surface at runtime — the hook just stops firing, or the agent silently fails to load.

Paths are checked RELATIVE TO THE REPO ROOT (derived from this file), not the absolute
paths baked into settings.json, so the suite is correct regardless of where the repo is
checked out (local `/home/ubuntu/server` vs CI `/home/runner/work/...`).

Run: uv run pytest .claude/scripts
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))  # .claude/scripts -> .claude -> repo root
CLAUDE = os.path.join(REPO, ".claude")

# Matches a repo-relative .claude/... script path inside a hook command string,
# regardless of the absolute prefix (~/server/..., /home/ubuntu/server/..., bash <path>).
_SCRIPT_RE = re.compile(r"\.claude/[\w./-]+\.(?:sh|py)")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _settings_hook_commands():
    """Yield every hook command string declared in .claude/settings.json."""
    settings = json.loads(_read(os.path.join(CLAUDE, "settings.json")))
    for event_groups in settings.get("hooks", {}).values():
        for group in event_groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command")
                if cmd:
                    yield cmd


def test_settings_hook_scripts_exist():
    """Every hook command in settings.json resolves to a script that exists in the repo."""
    commands = list(_settings_hook_commands())
    assert commands, "no hook commands found in settings.json — did the schema change?"
    missing = []
    for cmd in commands:
        m = _SCRIPT_RE.search(cmd)
        assert m, f"hook command has no recognizable .claude/ script path: {cmd!r}"
        rel = m.group(0)
        if not os.path.isfile(os.path.join(REPO, rel)):
            missing.append((rel, cmd))
    assert not missing, (
        f"settings.json references hook scripts that don't exist: {missing}"
    )


_PRUNE = {".git", ".venv", "node_modules", "__pycache__", "collections"}


def _repo_python_basenames():
    """All .py basenames in the repo, skipping vendored/build dirs."""
    names = set()
    for dirpath, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _PRUNE]
        for fn in files:
            if fn.endswith(".py"):
                names.add(fn)
    return names


def test_statusline_script_exists_if_configured():
    """If settings.json configures a statusLine command, its script must exist in the repo."""
    settings = json.loads(_read(os.path.join(CLAUDE, "settings.json")))
    sl = settings.get("statusLine")
    if not sl or sl.get("type") != "command":
        return  # no command statusLine configured — nothing to check
    cmd = sl.get("command", "")
    m = _SCRIPT_RE.search(cmd)
    assert m, f"statusLine command has no recognizable .claude/ script path: {cmd!r}"
    assert os.path.isfile(os.path.join(REPO, m.group(0))), (
        f"statusLine references a script that doesn't exist: {m.group(0)}"
    )


def test_hook_wrappers_reference_existing_python():
    """Each hooks/*.sh wrapper that invokes a .py must point at a file that exists.

    This is exactly the block-containers-edit -> block-protected-edits rename scenario:
    rename the .py but not the wrapper and the hook silently dies. Comment lines (`#`)
    are skipped — only actual invocations count. The .py may live in hooks/ (sibling
    invocation) or at repo-root scripts/ (e.g. validate-compose.sh), so membership is
    checked by basename across the whole repo.
    """
    py_names = _repo_python_basenames()
    hooks_dir = os.path.join(CLAUDE, "hooks")
    broken = []
    for fn in os.listdir(hooks_dir):
        if not fn.endswith(".sh"):
            continue
        for line in _read(os.path.join(hooks_dir, fn)).splitlines():
            if line.lstrip().startswith("#"):  # skip comments / doc mentions
                continue
            for pyref in re.findall(r"[\w./-]+\.py", line):
                if os.path.basename(pyref) not in py_names:
                    broken.append((fn, pyref))
    assert not broken, f".sh wrappers invoke missing python files: {broken}"


def _probe_subcommands():
    body = _read(os.path.join(REPO, "scripts", "probe.py"))
    return set(re.findall(r'\.add_parser\(\s*"([\w-]+)"', body))


def test_probe_subcommands_referenced_in_skills_and_agents_exist():
    """Every `probe.py <sub>` mentioned in a skill or agent is a real probe.py subparser."""
    valid = _probe_subcommands()
    assert "health" in valid and "ha" in valid, (
        f"probe.py subparser scan looks wrong: {valid}"
    )
    referenced = set()
    for sub in ("skills", "agents"):
        root = os.path.join(CLAUDE, sub)
        for dirpath, _, files in os.walk(root):
            for fn in files:
                if fn.endswith(".md"):
                    for name in re.findall(
                        r"probe\.py\s+([\w-]+)", _read(os.path.join(dirpath, fn))
                    ):
                        referenced.add(name)
    unknown = referenced - valid
    assert not unknown, (
        f"skills/agents reference unknown probe.py subcommands: {sorted(unknown)}"
    )


def _frontmatter_keys(path):
    """Return the set of top-level `key:` names in a leading --- YAML frontmatter block."""
    text = _read(path)
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end]
    return set(re.findall(r"^([A-Za-z][\w-]*):", block, re.MULTILINE))


def test_skills_and_agents_have_required_frontmatter():
    """Every SKILL.md and agent .md has the name+description frontmatter it needs to load."""
    targets = []
    skills_root = os.path.join(CLAUDE, "skills")
    for dirpath, _, files in os.walk(skills_root):
        if "SKILL.md" in files:
            targets.append(os.path.join(dirpath, "SKILL.md"))
    agents_root = os.path.join(CLAUDE, "agents")
    for fn in os.listdir(agents_root):
        if fn.endswith(".md"):
            targets.append(os.path.join(agents_root, fn))

    assert targets, "found no skills/agents to validate"
    bad = []
    for path in targets:
        keys = _frontmatter_keys(path)
        if keys is None or "name" not in keys or "description" not in keys:
            bad.append(os.path.relpath(path, REPO))
    assert not bad, (
        f"skills/agents missing required name/description frontmatter: {bad}"
    )


def test_rules_have_paths_frontmatter():
    """Every .claude/rules/*.md is path-scoped (has a `paths:` block) so it loads only when relevant."""
    rules_dir = os.path.join(CLAUDE, "rules")
    bad = []
    for fn in os.listdir(rules_dir):
        if not fn.endswith(".md"):
            continue
        keys = _frontmatter_keys(os.path.join(rules_dir, fn))
        if keys is None or "paths" not in keys:
            bad.append(fn)
    assert not bad, (
        f"rules missing `paths:` frontmatter (would load globally every session): {bad}"
    )
