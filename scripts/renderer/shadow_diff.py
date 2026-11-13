#!/usr/bin/env python3
"""Shadow-render tooling beside `homelab-render`: the stub file, a tree's digest, the round trip.

Stub values are never secrets, so a stub render needs no age key.

- `stubs` writes them as an extra-vars file: every secret key's stub and the
  sentinel `homelab_secret_stubs: true`, and nothing else. No template reads a live fact, so
  the file pins none. The file is a function of the git tree alone, so an agent, CI and the
  operator write the same bytes for one commit. It reads only the key NAMES of
  `secrets/secrets.yml`. SOPS leaves them in plaintext, so nothing is decrypted, and no
  value of that file reaches the output.
- `digest DIR` prints one sha256 line per directory under `DIR/cluster/`, then a `total` line.
  R1's determinism tests write the same lines in CI with `renderer_lib/shadow.py`'s
  functions.
- `roundtrip --out DIR` (target slice R2) renders each tag in placeholder mode and in stub
  mode, builds every directory of both with `flux build kustomization`, substituting each
  key's stub where a directory substitutes, and compares the outputs byte for byte. A second
  pass does the same with `0123` for every key the standalone-value guard does not cover,
  which shows a standalone use the guard's map misses. `--guard-map FILE` reads another map
  than the committed one, so `{}` makes every key `0123` again. Its library is
  `renderer_lib/roundtrip.py`.
- `fluxprobe --out DIR` builds small fixtures with the flux CLI and fails unless each escape
  comes back as the round trip relies on. Its library is `renderer_lib/flux_probe.py`.

`roundtrip` is the one subcommand that renders, through the renderer's own library, and it
and `fluxprobe` need the pinned flux CLI on `PATH`. `digest` lives in `renderer_lib/shadow.py`.
`docs/homelab-render.md` is the operator's page.

Exit codes: 0 done, with no difference; 1 a round-trip difference or a failed probe case;
2 refused, for a tree with no `cluster/`, an occupied --out, no flux on PATH, or a round trip
whose render failed or holds no directory that substitutes; 64 a usage error.

Run: uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
     uv run python scripts/renderer/shadow_diff.py digest RENDER_DIR
     uv run python scripts/renderer/shadow_diff.py fluxprobe --out /tmp/r2-probe
     uv run python scripts/renderer/shadow_diff.py roundtrip --out /tmp/r2-roundtrip \\
         --role configarr --role janitorr --role home-assistant
     uv run pytest scripts/renderer/tests
"""

import dataclasses
import sys
from collections.abc import Callable
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import yaml

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.repo_paths import SECRETS as SECRETS_DIR
from lib.exit_codes import SHADOW_OK, SHADOW_REFUSED
from renderer.renderer_lib.cluster import (
    RenderError,
    RenderRefused,
    select_tags,
)
from renderer.renderer_lib.flux_probe import probe
from renderer.renderer_lib.placeholders import GUARD_MAP, read_guard_map
from renderer.renderer_lib.roundtrip import (
    FluxMissing,
    Sides,
    StubPass,
    compare_sides,
    find_flux,
    guarded_keys,
    render_sides,
    stub_passes,
)
from renderer.renderer_lib.shadow import TreeRefused, digest_lines


from renderer.template_filters.secret_stubs import (
    homelab_secret_stub,
    homelab_stub_keys,
)

SECRETS = SECRETS_DIR / "secrets.yml"
SENTINEL = "homelab_secret_stubs"

_HEADER = (
    "# Written by scripts/renderer/shadow_diff.py stubs. Stub values only, for a k8s dry run\n"
    "# and homelab-render. Never pass it to a run that applies anything.\n"
)


def stub_vars(secrets: Path = SECRETS) -> dict[str, object]:
    """The extra vars of a stub run: every secret key's stub and the sentinel.

    Raises:
        ValueError: the sentinel shares a name with a secret key, so one value would silently
            replace the other.
    """
    # Parsed for its keys only. The values are SOPS ciphertext and are never read.
    keys = homelab_stub_keys(yaml_fast.safe_load(secrets.read_text()))
    stubs: dict[str, object] = {key: homelab_secret_stub(key) for key in keys}
    if SENTINEL in stubs:
        raise ValueError(f"{SENTINEL} is both a secret key and the stub-run sentinel")
    stubs[SENTINEL] = True
    return stubs


def stub_file_text(secrets: Path = SECRETS) -> str:
    """The stub file's bytes: a YAML mapping with sorted keys after a fixed header.

    Args:
        secrets: The secrets file whose key names the stubs are built from.
    """
    variables = stub_vars(secrets)
    body = yaml.safe_dump(
        variables, sort_keys=True, default_flow_style=False, width=4096
    )
    return _HEADER + body


# -- the CLI ------------------------------------------------------------------------------


def _refused(command: str, reason: str) -> int:
    print(f"shadow_diff {command}: refused: {reason}", file=sys.stderr)
    return SHADOW_REFUSED


def _digest(root: Path) -> int:
    try:
        lines = digest_lines(root)
    except TreeRefused as exc:
        return _refused("digest", str(exc))
    print("\n".join(lines))
    return SHADOW_OK


def red_proof(stub_pass: StubPass, sides: Sides) -> tuple[str, StubPass]:
    """`stub_pass` with one key's substitute value changed by one byte, and that key.

    The key is the first, in sorted order, of the first directory that substitutes, so a
    working round trip must report that directory.

    Raises:
        ValueError: No rendered directory substitutes, so no value can be changed.
    """
    for name in sides.placeholder.substituting():
        key = sorted(sides.placeholder.key_sets[name])[0]
        value = stub_pass.substitute[key]
        changed = value[:-1] + ("y" if value.endswith("x") else "x")
        substitute = {**stub_pass.substitute, key: changed}
        return key, dataclasses.replace(stub_pass, substitute=substitute)
    raise ValueError(
        "no rendered directory substitutes, so --red-proof has no value to change"
    )


def _roundtrip(
    out: Path,
    roles: list[str] | None,
    with_red_proof: bool,
    guard_map: Path,
    render: Callable[[Path, list[str], list[str], list[StubPass]], Sides],
) -> int:
    try:
        flux = find_flux()
        guarded = guarded_keys(read_guard_map(guard_map))
    except FluxMissing as exc:
        return _refused("roundtrip", str(exc))
    except (OSError, ValueError) as exc:
        return _refused("roundtrip", f"the guard map {guard_map} does not read: {exc}")
    keys = homelab_stub_keys(yaml_fast.safe_load(SECRETS.read_text()))
    passes = stub_passes(stub_vars(), keys, guarded)
    # Every failure before the builds is a refusal, never exit 1. CI's red half accepts only
    # exit 1, so a setup failure or an empty substituting set cannot pass as a red proof.
    try:
        sides = render(out, select_tags(roles), keys, passes)
        if not sides.placeholder.substituting():
            raise RenderRefused(
                "no rendered directory substitutes, so the round trip would prove nothing "
                "about substitution"
            )
        if with_red_proof:
            key, passes[0] = red_proof(passes[0], sides)
            print(f"roundtrip: red proof: {key}'s substitute value changed by one byte")
    except (RenderRefused, RenderError, ValueError) as exc:
        return _refused("roundtrip", str(exc))
    reports = [compare_sides(sides, p, flux) for p in passes]
    for report in reports:
        print("\n".join(report.lines()))
    return max(report.exit_code() for report in reports)


def _fluxprobe(out: Path, red_proof: bool) -> int:
    try:
        flux = find_flux()
    except FluxMissing as exc:
        return _refused("fluxprobe", str(exc))
    if out.exists() and any(out.iterdir()):
        return _refused("fluxprobe", f"{out} is not empty; probe into an empty --out")
    report = probe(out, flux, red_proof=red_proof)
    print("\n".join(report.lines))
    print(f"fluxprobe: {len(report.lines)} cases, {len(report.failures)} failed")
    return report.exit_code()


def main(
    argv: list[str] | None = None,
    render: Callable[
        [Path, list[str], list[str], list[StubPass]], Sides
    ] = render_sides,
) -> int:
    """Run one subcommand and return its exit code.

    Args:
        argv: The command line, or `sys.argv[1:]` when None.
        render: `roundtrip`'s `render_sides`. A test passes one that reuses a tag's renders
            across its tests, which a fresh process never could.
    """
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    stubs = commands.add_parser(
        "stubs", help="write the stub extra-vars file for a shadow render"
    )
    stubs.add_argument("--out", type=Path, required=True, help="the file to write")
    dig = commands.add_parser(
        "digest", help="print a sha256 per directory under DIR/cluster/, then a total"
    )
    dig.add_argument("dir", type=Path, metavar="DIR", help="a C1 tree")
    trip = commands.add_parser(
        "roundtrip",
        help="build the placeholder and stub renders with flux build and compare them",
    )
    trip.add_argument(
        "--out", type=Path, required=True, metavar="DIR", help="an empty directory"
    )
    trip.add_argument(
        "--role",
        dest="roles",
        action="extend",
        nargs="+",
        metavar="TAG",
        help="a deploy tag to round-trip; repeatable. Default: every k8s render target",
    )
    trip.add_argument(
        "--red-proof",
        action="store_true",
        help="change one key's substitute value by one byte, so a working round trip "
        "exits 1",
    )
    trip.add_argument(
        "--guard-map",
        type=Path,
        default=GUARD_MAP,
        metavar="FILE",
        help="the standalone-value guard's map whose keys keep their R1 stub in the second "
        "pass. Default: the committed map; `{}` sets every key to 0123",
    )
    fprobe = commands.add_parser(
        "fluxprobe", help="check the flux CLI's substitution on small fixtures"
    )
    fprobe.add_argument(
        "--out", type=Path, required=True, metavar="DIR", help="an empty directory"
    )
    fprobe.add_argument(
        "--red-proof",
        action="store_true",
        help="also expect an output no fixture yields, so a working probe exits 1",
    )
    args = parser.parse_args(argv)

    if args.command == "digest":
        return _digest(args.dir)
    if args.command == "roundtrip":
        return _roundtrip(args.out, args.roles, args.red_proof, args.guard_map, render)
    if args.command == "fluxprobe":
        return _fluxprobe(args.out, args.red_proof)
    args.out.write_text(stub_file_text())
    print(f"shadow_diff: wrote {args.out}")
    return SHADOW_OK


if __name__ == "__main__":
    sys.exit(main())
