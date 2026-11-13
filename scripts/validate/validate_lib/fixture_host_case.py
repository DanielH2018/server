#!/usr/bin/env python3
"""The fixture host the bootstrap cases build, from `docs/host-packages.md`, *Adding a host* (H7.15d).

`apply_walkthrough` makes a checkout take a host the way the walkthrough's step 1 lists: the
package spec from the page's own YAML block, the `host_vars` copy and the
`HOSTS` entry. `bootstrap_case.py` then runs `bootstrap-host.sh --local-build` on a container of
that name, so the script itself renders and builds the three packages and apt simulates the
install. A walkthrough whose file list stops rendering or building fails that case.

Steps 3 to 5 name the host in sets that nothing reads while a host builds.
`hosts/lib/tests/test_host_census.py` holds those.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
import shutil
import textwrap
from pathlib import Path

from validate.validate_lib.host_container import CaseFailed

FIXTURE_HOST = "daniel-fixture"
WALKTHROUGH = "docs/host-packages.md"
SCRIPT = "hosts/bootstrap-host.sh"
EXAMPLE_VARS = "ansible/inventory/host_vars/_example.yml"
# An address no host holds, for the example file's placeholder. Nothing dials it in a render.
FIXTURE_IP = "10.0.0.250"


def walkthrough_spec(doc: str, host: str) -> str:
    """The `package.yml` that *Adding a host* step 1 shows, for `host`.

    Raises:
        CaseFailed: The page has no such section, or the section holds no YAML block.
    """
    _, heading, section = doc.partition("\n## Adding a host\n")
    block = re.search(r"```yaml\n(.*?)^\s*```", section, re.DOTALL | re.MULTILINE)
    if not heading or not block:
        raise CaseFailed(f"{WALKTHROUGH}'s *Adding a host* holds no YAML block")
    return textwrap.dedent(block[1]).replace("<host>", host)


def _replace_one(text: str, pattern: str, replacement: str, what: str) -> str:
    changed, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise CaseFailed(f"{what} matched {count} lines, not one")
    return changed


def apply_walkthrough(tree: Path, host: str = FIXTURE_HOST) -> None:
    """Add `host` to the checkout at `tree`, as the walkthrough's step 1 lists.

    Raises:
        CaseFailed: A file no longer holds the line this edits.
    """
    spec = tree / "hosts" / host / "package.yml"
    spec.parent.mkdir()
    spec.write_text(walkthrough_spec((tree / WALKTHROUGH).read_text(), host))

    host_vars = tree / EXAMPLE_VARS
    copy = host_vars.with_name(f"{host}.yml")
    shutil.copyfile(host_vars, copy)
    copy.write_text(
        _replace_one(
            copy.read_text(),
            r"^server_ip: 10\.0\.0\.x$",
            f"server_ip: {FIXTURE_IP}",
            f"{EXAMPLE_VARS}'s server_ip",
        )
    )

    script = tree / SCRIPT
    script.write_text(
        _replace_one(
            script.read_text(),
            r'^HOSTS="(.*)"$',
            rf'HOSTS="\1 {host}"',
            f"{SCRIPT}'s HOSTS",
        )
    )
