"""cast_quit_app.py quits only the HA dashboard and reports a failure in its exit code.

pychromecast ships in HA's image, not this repo's environment, so a stub stands in for it in
`sys.modules`. The stub device records what the script asked of it; the tests read the exit
code and that record, which is what bedroom_display_dismiss and the user's media depend on.
"""

import sys
import types

import cast_quit_app
import pytest

DASHBOARD = "A078F6B0"


class _Device:
    def __init__(self, app_id: str, refuse: bool = False):
        self.app_id = app_id
        self.refuse = refuse
        self.quit_sent = False
        self.disconnected = False

    def wait(self, timeout: float) -> None:
        pass

    def quit_app(self, timeout: float) -> None:
        if self.refuse:
            raise RuntimeError("Failed to execute quit app.")
        self.quit_sent = True

    def disconnect(self, timeout: float) -> None:
        self.disconnected = True


@pytest.fixture
def device(monkeypatch):
    def install(app_id: str, refuse: bool = False) -> _Device:
        dev = _Device(app_id, refuse)
        pychromecast = types.SimpleNamespace(
            get_chromecast_from_host=lambda host, **kwargs: dev
        )
        config = types.SimpleNamespace(APP_HOMEASSISTANT_LOVELACE=DASHBOARD)
        monkeypatch.setitem(sys.modules, "pychromecast", pychromecast)
        monkeypatch.setitem(sys.modules, "pychromecast.config", config)
        return dev

    return install


def test_the_dashboard_is_quit(device):
    dev = device(DASHBOARD)
    assert cast_quit_app.main(["10.0.0.137"]) == 0
    assert dev.quit_sent and dev.disconnected


def test_another_app_is_left_running(device):
    dev = device("CC1AD845")
    assert cast_quit_app.main(["10.0.0.137"]) == 0
    assert not dev.quit_sent and dev.disconnected


def test_a_refused_quit_exits_non_zero(device, capsys):
    dev = device(DASHBOARD, refuse=True)
    assert cast_quit_app.main(["10.0.0.137"]) == 1
    assert "Failed to execute quit app" in capsys.readouterr().err
    assert dev.disconnected
