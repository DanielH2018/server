#!/usr/bin/env python3
"""Render, diff and lint the host packages: the checks behind CI's `host_packages` job.

H3's *CI (builds, never signs)* runs these in order. `render` writes every package a tree
holds, `diff` compares the render of the PR head with the render of its merge base, and the
job ends there when no rendered tree differs. Otherwise the job builds both renders with the
pinned nfpm, and `lint` checks what it rendered and what it built:

  * the `type: config` lint (H3's *Package rules*, plain files only): no `nfpm.yaml` content
    entry carries a `type` starting `config`, and no built `.deb` lists a conffile. A conffile
    survives an upgrade that drops or changes it, so the package would stop owning the file it
    ships.
  * the maintainer-script lint: every control script of a built `.deb` holds only the line
    shapes in `SCRIPT_WHITELIST`. `postinst` reloads systemd and starts reconcile without
    blocking, `prerm` stops the package's own `homelab-*` units only on `remove`, and `preinst`
    refuses a hostname mismatch. The whitelist admits no `dpkg-divert` line; H7's *In-place
    edits* adds those shapes with the first diversion.

`render` runs the renderer that ships in the tree it renders, so a renderer change that alters
a render shows in the diff. A tree with no `hosts/` directory predates H3.1 and renders no
package.

`fetch-tools` downloads, verifies and places the binaries each rendered `homelab-tools` tree
pins, so the job's nfpm step can build it. It runs the builder's own `install_tools`, the code
daniel-box builds the package with, so a pin whose digest or URL is wrong fails here before it
reaches master (H3.11). A digest that differs from its pin fails the command.

`job-changed` says whether one job of a workflow parses differently at a commit than in the
working tree. The job's own steps invoke the container cases, so a PR that edits them runs the
cases (#5004). The comparison is of the parsed YAML, so an edit to a comment alone does not
count.

`fixture-changed` says whether the container cases' fixture `homelab-daniel-pi` renders
differently in two trees, each with the renderer and the fixture that tree holds. The fixture
exercises renderer paths no real host package does yet, such as a drop-in and a dropped unit,
so a renderer change can alter the fixture and no real render. Such a PR runs the cases too
(#5066).

Usage:
    uv run python scripts/validate/host_packages.py render --tree . --out /tmp/head
    uv run python scripts/validate/host_packages.py diff /tmp/base /tmp/head --summary FILE
    uv run python scripts/validate/host_packages.py fetch-tools /tmp/head --cache /tmp/tools
    uv run python scripts/validate/host_packages.py lint /tmp/head /tmp/debs
    uv run python scripts/validate/host_packages.py job-changed BASE_SHA \
        --workflow .github/workflows/ci.yml --job host_packages
    uv run python scripts/validate/host_packages.py fixture-changed /tmp/base-tree .

Exit codes: 0 the render, diff or lint succeeded and the lint found nothing; 1 the lint found a
problem (each `[FAIL]` line on stderr names it), or nothing to lint, or a render failed; 64 a
usage error. `diff` exits 0 whether or not the renders differ, and prints `changed=true` or
`changed=false` on stdout for `$GITHUB_OUTPUT`. `job-changed` and `fixture-changed` exit 0 either way
and print `true` or `false` on stdout. Either exits non-zero when it cannot decide, and CI's
render step fails on that rather than reading it as `false` (#5071).
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import importlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.git import git
from lib.repo_paths import REPO

# The module the builder fetches tools with (stdlib only), which CI runs the same way.
_TOOL_FETCH_DIR = REPO / "ansible" / "roles" / "setup" / "homelab_builder" / "files"

_UNIT = r"homelab-[a-z0-9-]+\.(?:service|timer|socket|path)"
_HOST = r"[a-z0-9][a-z0-9-]*"
_COMMON = [r"#!/bin/sh", r"", r"#.*", r"exit 0"]
SCRIPT_WHITELIST = {
    "postinst": [
        r"systemctl daemon-reload \|\| true",
        r"systemctl --no-block start homelab-reconcile\.service \|\| true",
    ],
    "prerm": [
        r'if \[ "\$1" = remove \]; then',
        rf"    systemctl stop {_UNIT} \|\| true",
        r"fi",
    ],
    "preinst": [
        rf'if \[ "\$\(hostname\)" != "{_HOST}" \]; then',
        rf'    echo "homelab-{_HOST} installs only on {_HOST}" >&2',
        r"    exit 1",
        r"fi",
    ],
}

# Control members dpkg-deb writes for every package, which hold no script.
_CONTROL_DATA = frozenset({"control", "md5sums"})


def lint_script(name: str, text: str) -> list[str]:
    """Each line of maintainer script `name` that no whitelisted shape matches.

    A script name outside the whitelist is one finding on its own.
    """
    if name not in SCRIPT_WHITELIST:
        return [f"{name}: no maintainer script of this name is allowed"]
    shapes = [re.compile(shape) for shape in [*_COMMON, *SCRIPT_WHITELIST[name]]]
    return [
        f"{name}:{number}: {line!r}"
        for number, line in enumerate(text.splitlines(), 1)
        if not any(shape.fullmatch(line) for shape in shapes)
    ]


def lint_config_types(spec: str) -> list[str]:
    """Each content entry of an `nfpm.yaml` whose `type` makes it a conffile."""
    contents = (yaml_fast.safe_load(spec) or {}).get("contents") or []
    return [
        f"{entry.get('dst')}: type {entry['type']!r} ships a conffile; "
        "host packages ship plain files only"
        for entry in contents
        if str(entry.get("type", "")).startswith("config")
    ]


def lint_deb(deb: Path) -> list[str]:
    """Each conffile and each non-whitelisted maintainer-script line in a built `.deb`."""
    problems = []
    with tempfile.TemporaryDirectory() as control:
        subprocess.run(["dpkg-deb", "--control", str(deb), control], check=True)
        for member in sorted(Path(control).iterdir()):
            if member.name in _CONTROL_DATA:
                continue
            text = member.read_text()
            if member.name == "conffiles":
                problems.extend(
                    f"conffiles lists {path}; host packages ship plain files only"
                    for path in text.split()
                )
            else:
                problems.extend(lint_script(member.name, text))
    return problems


def fetch_tools(render: Path, cache: Path, opener=None) -> list[str]:
    """Fetch, verify and place the binaries of every `tools.json` tree under `render`.

    Returns one `[FAIL]` line per tree whose pins did not verify. `opener` replaces `urlopen`.
    A render with no such tree has nothing to fetch, which is how a merge base from before
    H3.11 reads.
    """
    if str(_TOOL_FETCH_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_FETCH_DIR))
    builder = importlib.import_module("homelab_tool_fetch")
    trees = sorted(pins.parent for pins in render.rglob(builder.PINS_FILE))
    problems = []
    for tree in trees:
        try:
            builder.install_tools(tree, cache, opener)
        except builder.Failure as exc:
            problems.append(f"[FAIL] {tree}: {exc}")
    return problems


def specs(tree: Path) -> list[tuple[str, str, str]]:
    """Each package `tree` holds, as the renderer's `(mode, flag, name)` arguments."""
    hosts = tree / "hosts"
    shared = [
        ("package", "--name", spec.parent.name)
        for spec in sorted(hosts.glob("packages/*/package.yml"))
    ]
    host = [
        ("host", "--host", spec.parent.name)
        for spec in sorted(hosts.glob("*/package.yml"))
    ]
    return shared + host


def render(tree: Path, out: Path) -> int:
    """Render every package in `tree` into `out` with `tree`'s own renderer."""
    if not (tree / "hosts").is_dir():
        print(f"{tree} has no hosts/ directory, so it renders no package")
        return 0
    found = specs(tree)
    if not found:
        # hosts/ exists but the globs matched nothing, so they no longer find the specs.
        print(f"[FAIL] {tree / 'hosts'}: holds no package.yml", file=sys.stderr)
        return 1
    renderer = tree / "scripts" / "renderer" / "homelab_render.py"
    for mode, flag, name in found:
        result = subprocess.run(
            [sys.executable, str(renderer), mode, flag, name, "--out", str(out)],
            cwd=tree,
            check=False,
        )
        if result.returncode != 0:
            print(
                f"[FAIL] {name}: the render exited {result.returncode}", file=sys.stderr
            )
            return 1
    return 0


def _packages(out: Path) -> dict[str, dict[str, tuple[bytes, int]]]:
    """Each rendered package directory under `out`, mapped to its files' bytes and modes."""
    packages = {}
    for spec in sorted(out.rglob("nfpm.yaml")):
        directory = spec.parent
        packages[str(directory.relative_to(out))] = {
            str(path.relative_to(directory)): _entry(path)
            for path in sorted(directory.rglob("*"))
            if path.is_file() or path.is_symlink()
        }
    return packages


def _entry(path: Path) -> tuple[bytes, int]:
    """A file's bytes and mode, or for an enable link, its target and its own mode.

    A host package's link to the updater's timer dangles in its render, so a link is never
    followed.
    """
    if path.is_symlink():
        return f"-> {path.readlink()}".encode(), path.lstat().st_mode & 0o7777
    return path.read_bytes(), path.stat().st_mode & 0o7777


def diff(base: Path, head: Path) -> list[str]:
    """The file-level difference between two renders, one Markdown list item per change."""
    old, new = _packages(base), _packages(head)
    lines = []
    for package in sorted(old.keys() | new.keys()):
        if package not in new:
            lines.append(f"- `{package}`: dropped")
            continue
        if package not in old:
            lines.append(f"- `{package}`: new, {len(new[package])} files")
            continue
        before, after = old[package], new[package]
        for path in sorted(before.keys() | after.keys()):
            if path not in after:
                change = "removed"
            elif path not in before:
                change = "added"
            elif before[path][0] != after[path][0]:
                change = "content changed"
            elif before[path][1] != after[path][1]:
                change = f"mode {before[path][1]:04o} -> {after[path][1]:04o}"
            else:
                continue
            lines.append(f"- `{package}/{path}`: {change}")
    return lines


def job_differs(before: str | None, after: str, job: str) -> bool:
    """Whether `job` differs between two texts of one workflow, compared as parsed YAML.

    Args:
        before: the workflow at the base, or None when the base has no such file.
        after: the workflow at the head.
        job: the job id under `jobs:`.
    """

    def parsed(text: str | None) -> object:
        if text is None:
            return None
        return (yaml_fast.safe_load(text) or {}).get("jobs", {}).get(job)

    return parsed(before) != parsed(after)


def job_changed(base: str, workflow: Path, job: str) -> bool:
    """Whether `job` in `workflow` differs between commit `base` and the working tree."""
    shown = git("show", f"{base}:{workflow.as_posix()}", check=False)
    before = shown.stdout if shown.returncode == 0 else None
    return job_differs(before, workflow.read_text(), job)


# Run by `tree`'s own interpreter in isolated mode, so that tree's `validate_lib` and renderer
# are the ones imported. The payload variant's payload is random bytes; an empty one keeps the
# render the same from run to run.
_RENDER_FIXTURES = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from validate_lib import host_fixtures

for variant in host_fixtures.VARIANTS:
    host_fixtures.render_fixture(variant, Path(sys.argv[2]) / variant, payload_bytes=0)
"""


def render_fixtures(tree: Path, out: Path) -> bool:
    """Render every variant of the cases' fixture in `tree` into `out`, with `tree`'s code.

    Returns False when `tree` holds no fixture module, so it renders nothing.

    Raises:
        subprocess.CalledProcessError: the render failed.
    """
    validate = tree / "scripts" / "validate"
    if not (validate / "validate_lib" / "host_fixtures.py").is_file():
        # stderr, because CI reads this command's stdout as the verdict.
        print(
            f"{tree} has no host_fixtures module, so it renders no fixture",
            file=sys.stderr,
        )
        return False
    subprocess.run(
        [sys.executable, "-I", "-c", _RENDER_FIXTURES, str(validate), str(out)],
        cwd=tree,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return True


def fixture_changed(base: Path, head: Path) -> bool:
    """Whether the cases' fixture renders differently in tree `base` than in tree `head`.

    A side with no fixture module counts as a change, so a doubt runs the cases.
    """
    with tempfile.TemporaryDirectory() as scratch:
        before, after = Path(scratch) / "base", Path(scratch) / "head"
        rendered = [render_fixtures(base, before), render_fixtures(head, after)]
        if not all(rendered):
            return True
        lines = diff(before, after)
    for line in lines:
        print(line, file=sys.stderr)
    return bool(lines)


def lint(paths: list[Path]) -> list[str]:
    """Every problem in each `nfpm.yaml` and `.deb` under `paths`, as `[FAIL]` lines."""
    problems, checked = [], 0
    for path in paths:
        for spec in sorted(path.rglob("nfpm.yaml")):
            checked += 1
            problems.extend(
                f"[FAIL] {spec}: {p}" for p in lint_config_types(spec.read_text())
            )
        for deb in sorted(path.rglob("*.deb")):
            checked += 1
            problems.extend(f"[FAIL] {deb}: {p}" for p in lint_deb(deb))
    if not checked:
        problems.append(
            f"[FAIL] {' '.join(map(str, paths))}: no nfpm.yaml or .deb to lint"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    render_cmd = commands.add_parser(
        "render", help="render every package of TREE into DIR"
    )
    render_cmd.add_argument("--tree", type=Path, required=True, metavar="TREE")
    render_cmd.add_argument("--out", type=Path, required=True, metavar="DIR")
    diff_cmd = commands.add_parser("diff", help="compare two renders")
    diff_cmd.add_argument("base", type=Path)
    diff_cmd.add_argument("head", type=Path)
    diff_cmd.add_argument(
        "--summary", type=Path, metavar="FILE", help="append the Markdown diff to FILE"
    )
    lint_cmd = commands.add_parser(
        "lint", help="lint every nfpm.yaml and .deb under PATH"
    )
    lint_cmd.add_argument("paths", nargs="+", type=Path, metavar="PATH")
    fetch_cmd = commands.add_parser(
        "fetch-tools", help="fetch and verify the binaries homelab-tools pins"
    )
    fetch_cmd.add_argument("render", type=Path, metavar="RENDER")
    fetch_cmd.add_argument("--cache", type=Path, required=True, metavar="DIR")
    job_cmd = commands.add_parser(
        "job-changed", help="whether a workflow job differs from its form at BASE"
    )
    job_cmd.add_argument("base", metavar="BASE")
    job_cmd.add_argument("--workflow", type=Path, required=True)
    job_cmd.add_argument("--job", required=True)
    fixture_cmd = commands.add_parser(
        "fixture-changed",
        help="whether the cases' fixture renders differently in BASE_TREE and HEAD_TREE",
    )
    fixture_cmd.add_argument("base_tree", type=Path, metavar="BASE_TREE")
    fixture_cmd.add_argument("head_tree", type=Path, metavar="HEAD_TREE")
    args = parser.parse_args(argv)

    if args.command == "render":
        return render(args.tree.resolve(), args.out.resolve())
    if args.command == "diff":
        lines = diff(args.base, args.head)
        report = "\n".join(
            ["### Host packages: rendered diff against the merge base", ""]
            + (lines or ["No rendered tree differs."])
        )
        print(report, file=sys.stderr)
        if args.summary:
            with args.summary.open("a") as summary:
                summary.write(report + "\n")
        print(f"changed={'true' if lines else 'false'}")
        return 0
    if args.command == "fetch-tools":
        problems = fetch_tools(args.render.resolve(), args.cache.resolve())
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    if args.command == "job-changed":
        changed = job_changed(args.base, args.workflow, args.job)
        print("true" if changed else "false")
        return 0
    if args.command == "fixture-changed":
        changed = fixture_changed(args.base_tree.resolve(), args.head_tree.resolve())
        print("true" if changed else "false")
        return 0
    problems = lint(args.paths)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
