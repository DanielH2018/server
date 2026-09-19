#!/usr/bin/env python3
"""Render the `hooks` key of `.claude/settings.json` from the hook files' own declarations.

Run: uv run python scripts/dev/gen_hook_settings.py [--check | --list]

WHY. The `hooks` object in `.claude/settings.json` was hand-written JSON, and two tests
watched it from one side only: `.claude/hooks/tests/test_hook_scripts_executable.py` asserts
that every REGISTERED script exists and is executable, and `test_project_settings_shape.py`
that every entry declares a timeout. Nothing asserted the reverse -- that every hook file on
disk is registered somewhere -- so a hook that existed but fired nowhere was a silent gap
(dotfiles issue #537, the server half of #528). Each hook now declares its own registrations
in a comment block the shell and Python both ignore, this script renders the `hooks` object
from those declarations, and `--check` fails on a file that declares nothing.

THE GRAMMAR is the one `bin/gen-hooks-lib.js` in the dotfiles repo reads, minus the chezmoi
keys (`when:` and a raw `command:` override) that a plain-JSON file has no use for. One block
per registration; a file may carry several (`auto-mode-bridge.sh` fires on two events). A
file a sibling invokes and no event registers carries `library` with the reason spelled out::

    # gen-hooks: register
    #   event: PreToolUse
    #   matcher: Bash                   optional; omitted for an event with no matcher
    #   timeout: 10                     required, seconds
    #   order: 10                       required, unique within the event
    #   args: --flag                    optional, appended to the command
    #   async: true                     optional
    #   statusMessage: Formatting...    optional

    # gen-hooks: library
    #   reason: run by block-footguns.sh through `uv run python`

The opener is the only delimiter: a block runs from its `# gen-hooks:` line to the first line
that is not a `#   key: value` continuation. In a Python hook the block sits between the
shebang and the module docstring, as a comment, so the docstring stays the docstring.

THE CENSUS is every `*.sh` and `*.py` directly under `.claude/hooks/`, by suffix and not by
the exec bit, non-recursive. The bit is the property with the recorded history of drifting:
`uv-python.sh` shipped 100644 in #361, which is the incident `test_hook_scripts_executable.py`
exists for, and a new hook committed the same way would be exactly the file a bit-based
census skips. It is also incoherent on the Python modules here (five of the twelve are
100755, all twelve are run by a `.sh` shim through `uv run python`, none is ever exec'd).
`tests/` and `hooklib/` are one level down and are reached by import, never by a
registration.

THE SPLICE rewrites the whole file through `json.loads` / `json.dumps(indent=2)` with only
the `hooks` value replaced. That round trip is byte-identical to the committed file (measured
before this script existed), the dict keeps `hooks` where it sits, and JSON cannot carry a
begin/end marker comment, so there is nothing to splice between. A sibling
`hooks.generated.json` was the other shape and loses: Claude Code merges `settings.json` and
`settings.local.json`, not an arbitrary third file.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import REPO

SELF = "scripts/dev/gen_hook_settings.py"
HOOKS_DIR = REPO / ".claude" / "hooks"
SETTINGS = REPO / ".claude" / "settings.json"

# What a registration's `command` starts with. `test_hook_scripts_executable.py` anchors its
# own census on `^(?:~/server/|\./)`, so any other prefix empties that test's census.
COMMAND_PREFIX = "~/server/.claude/hooks/"

OPENER_RE = re.compile(r"^# gen-hooks: (\S+)\s*$")
FIELD_RE = re.compile(r"^#   ([a-zA-Z]+): (.*)$")

REGISTER_KEYS = frozenset(
    {"event", "matcher", "timeout", "order", "args", "async", "statusMessage"}
)
LIBRARY_KEYS = frozenset({"reason"})

# The order events are written in: the order the hand-written object had, so the first
# generated commit is a no-op diff. An event outside this list would register a hook that
# never fires, so it is an error, not a new key.
EVENT_ORDER = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionDenied",
    "InstructionsLoaded",
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "Notification",
    "PreCompact",
    "PostCompact",
    "Stop",
    "StopFailure",
    "SubagentStop",
    "SessionEnd",
)


class HookDeclarationError(ValueError):
    """A hook file's `# gen-hooks:` block is malformed, or a file declares nothing."""


@dataclass(frozen=True)
class Registration:
    file: str
    event: str
    timeout: int
    order: int
    matcher: str | None = None
    args: str | None = None
    async_: bool = False
    status_message: str | None = None

    @property
    def command(self) -> str:
        base = f"{COMMAND_PREFIX}{self.file}"
        return f"{base} {self.args}" if self.args else base

    def entry(self) -> dict:
        out: dict = {
            "type": "command",
            "command": self.command,
            "timeout": self.timeout,
        }
        if self.status_message:
            out["statusMessage"] = self.status_message
        if self.async_:
            out["async"] = True
        return out


@dataclass
class Parsed:
    registrations: list[Registration] = field(default_factory=list)
    library_reason: str | None = None


def _int_field(name: str, value: str, file: str) -> int:
    if not re.fullmatch(r"\d+", value):
        raise HookDeclarationError(
            f"gen-hooks: {file}: {name} must be a non-negative integer, got '{value}'."
        )
    return int(value)


def parse_hook_file(file: str, text: str) -> Parsed:
    """Every block in one hook file. Raises HookDeclarationError on a malformed one."""
    lines = text.split("\n")
    parsed = Parsed()
    i = 0
    while i < len(lines):
        opener = OPENER_RE.match(lines[i])
        if not opener:
            i += 1
            continue
        kind = opener.group(1)
        if kind not in ("register", "library"):
            raise HookDeclarationError(
                f"gen-hooks: {file}:{i + 1}: unknown block '# gen-hooks: {kind}' "
                "(expected register or library)."
            )
        allowed = REGISTER_KEYS if kind == "register" else LIBRARY_KEYS
        fields: dict[str, str] = {}
        j = i + 1
        while j < len(lines):
            m = FIELD_RE.match(lines[j])
            if not m:
                break
            key, value = m.group(1), m.group(2).strip()
            if key not in allowed:
                raise HookDeclarationError(
                    f"gen-hooks: {file}:{j + 1}: unknown key '{key}' in a {kind} block."
                )
            if key in fields:
                raise HookDeclarationError(
                    f"gen-hooks: {file}:{j + 1}: duplicate key '{key}'."
                )
            fields[key] = value
            j += 1
        block_line = i + 1
        i = j
        if kind == "library":
            if not fields.get("reason"):
                raise HookDeclarationError(
                    f"gen-hooks: {file}: a library block needs a reason: line."
                )
            if parsed.library_reason is not None:
                raise HookDeclarationError(
                    f"gen-hooks: {file}: more than one library block."
                )
            parsed.library_reason = fields["reason"]
            continue
        for req in ("event", "timeout", "order"):
            if not fields.get(req):
                raise HookDeclarationError(
                    f"gen-hooks: {file}: register block at line {block_line} is "
                    f"missing {req}:."
                )
        if fields["event"] not in EVENT_ORDER:
            raise HookDeclarationError(
                f"gen-hooks: {file}: unknown event '{fields['event']}'. "
                f"Known: {', '.join(EVENT_ORDER)}."
            )
        if "async" in fields and fields["async"] != "true":
            raise HookDeclarationError(
                f"gen-hooks: {file}: async: takes only 'true' (omit the line otherwise)."
            )
        parsed.registrations.append(
            Registration(
                file=file,
                event=fields["event"],
                timeout=_int_field("timeout", fields["timeout"], file),
                order=_int_field("order", fields["order"], file),
                matcher=fields.get("matcher") or None,
                args=fields.get("args") or None,
                async_="async" in fields,
                status_message=fields.get("statusMessage") or None,
            )
        )
    if parsed.library_reason is not None and parsed.registrations:
        raise HookDeclarationError(
            f"gen-hooks: {file}: is marked library but also carries a register block."
        )
    return parsed


def hook_files(hooks_dir: Path = HOOKS_DIR) -> dict[str, str]:
    """The census: `{basename: text}` for every *.sh / *.py directly under `hooks_dir`."""
    return {
        p.name: p.read_text()
        for p in sorted(hooks_dir.iterdir())
        if p.is_file() and p.suffix in (".sh", ".py")
    }


def census(files: dict[str, str]) -> tuple[list[Registration], dict[str, str]]:
    """Every registration plus `{file: reason}` for the libraries.

    Raises for a file that declares nothing: that is the drift this script exists to close.
    """
    registrations: list[Registration] = []
    libraries: dict[str, str] = {}
    undeclared: list[str] = []
    for file in sorted(files):
        parsed = parse_hook_file(file, files[file])
        if parsed.library_reason is not None:
            libraries[file] = parsed.library_reason
        elif not parsed.registrations:
            undeclared.append(file)
        registrations.extend(parsed.registrations)
    if undeclared:
        raise HookDeclarationError(
            f"gen-hooks: {len(undeclared)} hook file(s) declare no registration and are "
            f"not marked library, so they would fire nowhere: {', '.join(undeclared)}. "
            "Add a `# gen-hooks: register` block (or `# gen-hooks: library` with a reason:)."
        )
    return registrations, libraries


def render_hooks(registrations: list[Registration]) -> dict:
    """The `hooks` object, in the shape the hand-written one had.

    Events in EVENT_ORDER, entries by `order`, consecutive entries sharing a matcher
    folded into one matcher group.
    """
    by_event: dict[str, list[Registration]] = {}
    for r in registrations:
        by_event.setdefault(r.event, []).append(r)
    hooks: dict = {}
    for event in EVENT_ORDER:
        regs = by_event.get(event)
        if not regs:
            continue
        regs = sorted(regs, key=lambda r: r.order)
        for prev, cur in zip(regs, regs[1:], strict=False):
            if prev.order == cur.order:
                raise HookDeclarationError(
                    f"gen-hooks: {event}: {prev.file} and {cur.file} both declare order: "
                    f"{cur.order}. Orders are unique within an event."
                )
        groups: list[dict] = []
        for r in regs:
            if groups and groups[-1].get("matcher") == r.matcher:
                groups[-1]["hooks"].append(r.entry())
            else:
                group: dict = {}
                if r.matcher is not None:
                    group["matcher"] = r.matcher
                group["hooks"] = [r.entry()]
                groups.append(group)
        hooks[event] = groups
    return hooks


def render_settings(settings_text: str, registrations: list[Registration]) -> str:
    """`settings_text` with only its `hooks` value replaced.

    Re-serialised the way the committed file is: 2-space indent, trailing newline.
    """
    data = json.loads(settings_text)
    data["hooks"] = render_hooks(registrations)
    return json.dumps(data, indent=2) + "\n"


def main(
    argv: list[str] | None = None,
    hooks_dir: Path = HOOKS_DIR,
    settings: Path = SETTINGS,
) -> int:
    """The CLI.

    `hooks_dir` and `settings` are overridable so a test can run the whole path against a
    scratch tree; the defaults are the repo's own.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed settings.json differs from what this renders now",
    )
    mode.add_argument(
        "--list",
        action="store_true",
        help="print the census: one line per registration, then the libraries",
    )
    args = parser.parse_args(argv)
    try:
        registrations, libraries = census(hook_files(hooks_dir))
        summary = f"{len(registrations)} registrations, {len(libraries)} libraries"
        if args.list:
            for r in registrations:
                print(f"{r.event}\t{r.matcher or '*'}\t{r.order}\t{r.command}")
            for file, reason in libraries.items():
                print(f"library\t{file}\t{reason}")
            print(summary)
            return 0
        current = settings.read_text()
        regenerated = render_settings(current, registrations)
    except (HookDeclarationError, json.JSONDecodeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    rel = settings.relative_to(REPO) if settings.is_relative_to(REPO) else settings
    if args.check:
        if regenerated == current:
            print(f"gen-hooks --check: {rel} is up to date ({summary}).")
            return 0
        print(
            f"gen-hooks --check: {rel} is out of date. Run `uv run python {SELF}` and "
            "commit the result.",
            file=sys.stderr,
        )
        return 1
    if regenerated != current:
        settings.write_text(regenerated)
        print(f"gen-hooks: wrote {rel} ({summary}).")
    else:
        print(f"gen-hooks: {rel} already up to date ({summary}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
