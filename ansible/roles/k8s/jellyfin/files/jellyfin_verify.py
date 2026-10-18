"""jellyfin's verify Job: prove the render node opens and the library sits where its database looks.

Inert until jellyfin's flip: `templates/verify-inert/` holds the Job, and the flip PR moves it
to `templates/verify/`, where the renderer emits it into `jellyfin-verify` (target slice D5, *A
verify conversion lands as inert code*). It replaces two of `tasks/verify.yml`'s four `kubectl
exec` checks. The Job's pod requests the same `devic.es/dri` device, adds the same render-node
group, mounts `media-data` read-only at the same two paths and runs as jellyfin's PUID and PGID.
So it proves the wiring of a pod built like jellyfin's, not of the live jellyfin pod, which
nothing here can reach without `pods/exec`.

The library check asserts both directions, as the Ansible task did. The library roots must be
present, and `torrents` must be absent from the mount: its presence is the signature of
mounting the volume root instead of its `media` subdirectory. Then it reads 1 KiB of the first
`.mkv` under the TV root, which is what a failing playback was doing when it raised its IO
error.

The other two checks, a VAAPI encode and the HDR tone-mapping path through jellyfin's own
ffmpeg, have no form this Job can take. The Job's image carries no ffmpeg, and the Job census
forbids spawning a process and opening a socket, so an exec through the API is not open to it
either. Ansible runs both until the flip; after it nothing runs them unless the operator's
choice in #5451 lands first.

The Job reaches no Service and no API, so it imports no `flux_job_connect`. Inputs come from
the Job's environment: `VERIFY_DEVICE`, the render node, and `VERIFY_DATA`, the library mount.
`JELLYFIN_DEPLOYMENT_DIGEST8` is in the spec only to rename the Job, and the script prints it.
Exit 0 when both checks pass, 1 when either fails, naming it.

Run: python3 jellyfin_verify.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/jellyfin/tests/test_jellyfin_verify.py
"""

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path

# The library roots Jellyfin's database stores, relative to its `/data` mount. `media/tv` is
# the second mount, which janitorr's leaving-soon symlinks resolve through.
LIBRARY_ROOTS = ("tv", "movies", "media/tv")
# Present only when the volume root is mounted instead of its `media` subdirectory.
ROOT_ONLY = "torrents"
# The library root a real file is read from, and how much of it.
READ_ROOT = "tv"
READ_BYTES = 1024


class Failed(Exception):
    """A check failed; the message names what is missing."""


def check_device(device: Path) -> str:
    """Open `device` read-only and close it without reading.

    A read blocks forever on a DRM render node once `open()` succeeds, which is why the
    Ansible task it replaces ran `: < dev`.
    """
    try:
        fd = os.open(device, os.O_RDONLY)
    except OSError as err:
        raise Failed(f"cannot open the render node {device}: {err.strerror}") from err
    os.close(fd)
    return f"{device} opens"


def _first_mkv(root: Path) -> Path | None:
    for parent, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.endswith(".mkv"):
                return Path(parent) / name
    return None


def check_library(data: Path) -> str:
    """Prove the library roots are at `data`, the volume root is not, and a file reads."""
    absent = [root for root in LIBRARY_ROOTS if not (data / root).is_dir()]
    if absent:
        raise Failed(f"the library roots {absent} are not under {data}")
    if (data / ROOT_ONLY).exists():
        raise Failed(
            f"{data / ROOT_ONLY} exists, so {data} mounts the volume root, not its media "
            "subdirectory"
        )
    sample = _first_mkv(data / READ_ROOT)
    if sample is None:
        raise Failed(f"no .mkv file under {data / READ_ROOT} to read")
    try:
        with sample.open("rb") as handle:
            handle.read(READ_BYTES)
    except OSError as err:
        raise Failed(f"cannot read {sample}: {err.strerror}") from err
    return f"the library is at {data}; read {READ_BYTES} bytes of {sample}"


def run(env: Mapping[str, str]) -> list[str]:
    """Run both checks, and return one report line each.

    Raises:
        Failed: The first check that failed.
    """
    return [
        "verifying jellyfin's Deployment "
        f"{env.get('JELLYFIN_DEPLOYMENT_DIGEST8', 'unknown')}",
        check_device(Path(env["VERIFY_DEVICE"])),
        check_library(Path(env["VERIFY_DATA"])),
    ]


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    try:
        lines = run(os.environ if env is None else env)
    except Failed as err:
        print(f"jellyfin-verify: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"jellyfin-verify: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
