#!/usr/bin/env python3
"""Shadow-render tooling beside `homelab-render`: the stub file both halves render with.

Target slice R1 compares the renderer with the Ansible manifests role for identical inputs.
Those inputs are stub values, never secrets, so the comparison needs no age key on either
side. `stubs` writes them as an Ansible extra-vars file: every secret key's stub, the
sentinel `homelab_secret_stubs: true`, and the fixed stand-ins of the live-read facts in
`PINNED_LIVE_FACTS`. The file is a function of the git tree alone, so an agent, CI and the
operator write the same bytes for one commit.

It reads only the key NAMES of `ansible/vars/secrets.yml`. SOPS leaves them in plaintext, so
nothing is decrypted, and no value of that file reaches the output.

This tool renders nothing. Later R1 PRs add `compare`, `stubcheck` and `digest` here.
`docs/homelab-render.md` is the operator's page.

Run: uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
     uv run pytest scripts/renderer/tests
"""

import sys
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import yaml

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.repo_paths import ANSIBLE, FILTER_PLUGINS
from renderer.renderer_lib.task_inputs import PINNED_LIVE_FACTS

# Ansible loads a filter plugin by file path; a script reaches it through this insert.
if str(FILTER_PLUGINS) not in _sys.path:
    _sys.path.insert(0, str(FILTER_PLUGINS))

from secret_stubs import homelab_secret_stub, homelab_stub_keys

SECRETS = ANSIBLE / "vars" / "secrets.yml"
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


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    stubs = commands.add_parser(
        "stubs", help="write the stub extra-vars file for a shadow render"
    )
    stubs.add_argument("--out", type=Path, required=True, help="the file to write")
    args = parser.parse_args(argv)

    args.out.write_text(stub_file_text())
    print(f"shadow_diff: wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
