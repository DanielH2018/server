#!/usr/bin/env python3
"""Shadow-render tooling beside `homelab-render`: the stub file, and the comparison of two renders.

Target slice R1 compares the renderer with the Ansible manifests role for identical inputs.
Those inputs are stub values, never secrets, so the comparison needs no age key on either
side.

- `stubs` writes them as an Ansible extra-vars file: every secret key's stub, the sentinel
  `homelab_secret_stubs: true`, and the fixed stand-ins of the live-read facts in
  `PINNED_LIVE_FACTS`. The file is a function of the git tree alone, so an agent, CI and the
  operator write the same bytes for one commit. It reads only the key NAMES of
  `ansible/vars/secrets.yml`. SOPS leaves them in plaintext, so nothing is decrypted, and no
  value of that file reaches the output.
- `compare A B` compares two render trees file by file. Either side is an Ansible shadow tree,
  one directory per service as `manifests_shadow_dir` keeps it, or a C1 tree, with `cluster/`
  at the top as `homelab_render.py cluster` writes it. It exits 0 only when every expected
  tag's files match, and it refuses an empty set and a missing named member.
- `stubcheck ANSIBLE C1` names each file whose Ansible render lacks a stub token that the C1
  render of the same file holds. It gates the operator's copy of the Ansible tree, and
  `compare` withholds the content of every file that fails it.
- `digest DIR` prints one sha256 line per directory under `DIR/cluster/`, then a `total` line.
  R1's determinism tests write the same lines in CI with `renderer_lib/shadow.py`'s
  functions.

None of them renders anything. `compare`'s report is for posting, so it prints no stub or
secret value: it names files, and the diff hunks it prints carry every stub token as `<stub>`.
`stubcheck` names the stub tokens a file lacks, never the file's content. The three comparison
subcommands live in `renderer_lib/shadow.py`. `docs/homelab-render.md` is the operator's page.

Exit codes: 0 done, with no difference; 1 a difference, or a file lacking a stub; 2 refused,
for an empty set, a missing named member or a tree of the wrong layout; 64 a usage error.

Run: uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
     uv run python scripts/renderer/shadow_diff.py compare ANSIBLE_DIR RENDER_DIR
     uv run python scripts/renderer/shadow_diff.py digest RENDER_DIR
     uv run pytest scripts/renderer/tests
"""

import sys
from collections.abc import Callable
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import yaml

from deploy_tools.render_targets import render_targets
from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST
from lib.cli_help import ArgumentParser
from lib.render_guard import containers_entries
from lib.repo_paths import ANSIBLE, FILTER_PLUGINS, HOST_VARS
from lib.exit_codes import SHADOW_DIFFERENT, SHADOW_OK, SHADOW_REFUSED
from renderer.renderer_lib.shadow import (
    TreeRefused,
    compare,
    digest_lines,
    load_tree,
    stubcheck,
)
from renderer.renderer_lib.task_inputs import PINNED_LIVE_FACTS

# Ansible loads a filter plugin by file path; a script reaches it through this insert.
if str(FILTER_PLUGINS) not in _sys.path:
    _sys.path.insert(0, str(FILTER_PLUGINS))

from secret_stubs import homelab_secret_stub, homelab_stub_keys

SECRETS = ANSIBLE / "vars" / "secrets.yml"
# The render targets, and those of them that are flux-owned.
TagSets = tuple[list[str], set[str]]
SENTINEL = "homelab_secret_stubs"

_HEADER = (
    "# Written by scripts/renderer/shadow_diff.py stubs. Stub values only, for a k8s dry run\n"
    "# and homelab-render. Never pass it to a run that applies anything.\n"
)


def stub_vars(secrets: Path = SECRETS) -> dict[str, object]:
    """The extra vars of a stub run: every secret stub, the sentinel and the pinned facts.

    Raises:
        ValueError: a pinned fact or the sentinel shares a name with a secret key, so one
            value would silently replace the other.
    """
    # Parsed for its keys only. The values are SOPS ciphertext and are never read.
    keys = homelab_stub_keys(yaml_fast.safe_load(secrets.read_text()))
    stubs: dict[str, object] = {key: homelab_secret_stub(key) for key in keys}
    clashes = sorted({SENTINEL, *PINNED_LIVE_FACTS} & stubs.keys())
    if clashes:
        raise ValueError(f"{clashes} are both secret keys and stub-run variables")
    stubs[SENTINEL] = True
    stubs.update({name: fact.value for name, fact in PINNED_LIVE_FACTS.items()})
    return stubs


def stub_file_text(secrets: Path = SECRETS) -> str:
    """The stub file's bytes: a YAML mapping with sorted keys after a fixed header.

    `google_assistant_service_account`'s stub is a JSON object string. The dumper quotes it,
    so `-e @FILE` loads a string, as the template's `from_json` expects, not a mapping.
    """
    body = yaml.safe_dump(
        stub_vars(secrets), sort_keys=True, default_flow_style=False, width=4096
    )
    return _HEADER + body


def tag_sets(host: str = K8S_HOST, host_vars: Path = HOST_VARS) -> TagSets:
    """`host`'s render targets, and those of them whose entry carries `owner: flux` (C4)."""
    targets = render_targets(host, host_vars=host_vars)
    path = host_vars / f"{host}.yml"
    flux = (
        {e["name"] for e in containers_entries(path) if e.get("owner") == "flux"}
        if path.exists()
        else set()
    )
    return targets, flux & set(targets)


# -- the CLI ------------------------------------------------------------------------------


def _refused(command: str, reason: str) -> int:
    print(f"shadow_diff {command}: refused: {reason}", file=sys.stderr)
    return SHADOW_REFUSED


def _compare(first: Path, second: Path, tags: Callable[[], TagSets]) -> int:
    try:
        trees = load_tree(first), load_tree(second)
    except TreeRefused as exc:
        return _refused("compare", str(exc))
    targets, flux = tags()
    report = compare(*trees, targets, flux)
    print("\n".join(report.lines()))
    return report.exit_code()


def _stubcheck(ansible_dir: Path, c1_dir: Path) -> int:
    try:
        ansible, c1 = load_tree(ansible_dir), load_tree(c1_dir)
    except TreeRefused as exc:
        return _refused("stubcheck", str(exc))
    if not ansible.ansible or c1.ansible:
        return _refused(
            "stubcheck", "pass an Ansible shadow tree first and a C1 tree second"
        )
    paired = ansible.files.keys() & c1.files.keys()
    if not paired:
        return _refused("stubcheck", "the two trees share no file")
    failing = stubcheck(ansible, c1)
    for key, lacking in failing.items():
        print(f"{key}: lacks {', '.join(lacking)}")
    print(f"{len(paired)} paired files, {len(failing)} lacking a stub token")
    return SHADOW_DIFFERENT if failing else SHADOW_OK


def _digest(root: Path) -> int:
    try:
        lines = digest_lines(root)
    except TreeRefused as exc:
        return _refused("digest", str(exc))
    print("\n".join(lines))
    return SHADOW_OK


def main(argv: list[str] | None = None, tags: Callable[[], TagSets] = tag_sets) -> int:
    """Run one subcommand and return its exit code.

    Args:
        argv: The command line, or `sys.argv[1:]` when None.
        tags: Returns the render targets and the flux-owned ones `compare` reads. The
            default reads the inventory; a test passes fixture sets.
    """
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    stubs = commands.add_parser(
        "stubs", help="write the stub extra-vars file for a shadow render"
    )
    stubs.add_argument("--out", type=Path, required=True, help="the file to write")
    cmp = commands.add_parser(
        "compare", help="compare two render trees over the expected tag set"
    )
    cmp.add_argument("first", type=Path, help="an Ansible shadow tree or a C1 tree")
    cmp.add_argument("second", type=Path, help="an Ansible shadow tree or a C1 tree")
    check = commands.add_parser(
        "stubcheck", help="name each Ansible file lacking a stub its C1 render holds"
    )
    check.add_argument("ansible", type=Path, help="the Ansible shadow tree")
    check.add_argument("c1", type=Path, help="the C1 tree homelab_render.py wrote")
    dig = commands.add_parser(
        "digest", help="print a sha256 per directory under DIR/cluster/, then a total"
    )
    dig.add_argument("dir", type=Path, metavar="DIR", help="a C1 tree")
    args = parser.parse_args(argv)

    if args.command == "compare":
        return _compare(args.first, args.second, tags)
    if args.command == "stubcheck":
        return _stubcheck(args.ansible, args.c1)
    if args.command == "digest":
        return _digest(args.dir)
    args.out.write_text(stub_file_text())
    print(f"shadow_diff: wrote {args.out}")
    return SHADOW_OK


if __name__ == "__main__":
    sys.exit(main())
