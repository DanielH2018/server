"""tdarr's verify Job: prove the render node opens and the media library is writable.

Inert until tdarr's flip: `templates/verify-inert/` holds the Job, and the flip PR moves it to
`templates/verify/`, where the renderer emits it into `tdarr-verify` (target slice D5, *A
verify conversion lands as inert code*). It replaces two of `tasks/verify.yml`'s three
`kubectl exec` checks. The Job's pod requests the same `devic.es/dri` device, the same
render-node group and the same `media-data` subPath as tdarr's Deployment, and runs as tdarr's
PUID and PGID, the uid tdarr's own process writes the library as. So it proves the wiring of a
pod built like tdarr's, not of the live tdarr pod, which nothing here can reach without
`pods/exec`.

The third check, an encode through tdarr's own ffmpeg, stays an Ansible task: this Job's image
carries no ffmpeg, and the Job census forbids spawning a process.

The Job reaches no Service and no API, so it opens no connection and imports no
`flux_job_connect`. Inputs come from the Job's environment: `VERIFY_DEVICE`, the render node,
and `VERIFY_MEDIA`, the library mount. The probe file is named after the pod's `HOSTNAME`, so
two runs never collide. Exit 0 when both checks pass, 1 when either fails, naming it.

Run: python3 tdarr_verify.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/tdarr/tests/test_tdarr_verify.py
"""

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path


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


def check_media(media: Path, probe: str) -> str:
    """Prove `media` is a directory this uid can create and remove a file in.

    A read-only or absent library reads in tdarr's UI as "no work to do" rather than as a
    fault, so the Job fails it here.
    """
    if not media.is_dir():
        raise Failed(f"the library {media} is not mounted")
    path = media / probe
    try:
        path.touch(exist_ok=False)
        path.unlink()
    except OSError as err:
        raise Failed(f"the library {media} is not writable: {err.strerror}") from err
    entries = sorted(p.name for p in media.iterdir())[:5]
    return f"{media} is writable; first entries: {entries}"


def run(env: Mapping[str, str]) -> list[str]:
    """Run both checks, and return one report line each.

    Raises:
        Failed: The first check that failed.
    """
    probe = f".tdarr-verify-{env.get('HOSTNAME', 'probe')}"
    return [
        check_device(Path(env["VERIFY_DEVICE"])),
        check_media(Path(env["VERIFY_MEDIA"]), probe),
    ]


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    try:
        lines = run(os.environ if env is None else env)
    except Failed as err:
        print(f"tdarr-verify: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"tdarr-verify: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
