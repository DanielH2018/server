"""Quit the Home Assistant dashboard on a Cast device over a connection of its own.

HA's cast integration runs pychromecast's Home Assistant controller on its connection, and that
controller can hold the socket thread for 30 s at a time, so HA's own `media_player.turn_off`
times out while the dashboard is up (#8201). A connection opened here carries no such
controller, so the device answers its quit. `bedroom_display_dismiss` runs this through
`shell_command.bedroom_display_quit_app` when its own turn_off has not taken effect.

Usage:

    python3 cast_quit_app.py HOST

Exits 0 once the device confirms the quit, and also when the dashboard is not the running app:
HA's view of the device can be stale, so this reads the running app itself and never stops
anything else. Exits 1, with the reason on stderr, when the device cannot be reached or refuses
the quit. Each step waits at most TIMEOUT_S, so a run stays well inside shell_command's 60 s.
"""

import argparse
import sys
import uuid

TIMEOUT_S = 10.0
CAST_PORT = 8009


def quit_dashboard(host: str) -> str:
    """Quit the HA dashboard on the Cast device at host, and say what was done.

    Raises:
        Exception: whatever pychromecast raises when the device is unreachable or refuses.
    """
    # Imported here so --help answers in an environment without pychromecast; HA's image has it.
    import pychromecast
    from pychromecast.config import APP_HOMEASSISTANT_LOVELACE

    cast = pychromecast.get_chromecast_from_host(
        (host, CAST_PORT, uuid.uuid4(), None, None),
        tries=1,
        retry_wait=1.0,
        timeout=TIMEOUT_S,
    )
    try:
        cast.wait(timeout=TIMEOUT_S)
        if cast.app_id != APP_HOMEASSISTANT_LOVELACE:
            return f"left alone: the running app is {cast.app_id}"
        cast.quit_app(timeout=TIMEOUT_S)
        return "quit the dashboard"
    finally:
        cast.disconnect(timeout=TIMEOUT_S)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="Exit 0: quit, or the dashboard was not running. Exit 1: unreachable or refused.",
    )
    parser.add_argument("host", help="the Cast device's address")
    args = parser.parse_args(argv)
    try:
        print(f"{args.host}: {quit_dashboard(args.host)}")
    except Exception as err:
        print(f"{args.host}: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
