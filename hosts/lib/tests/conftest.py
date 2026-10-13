"""The `host` fixture: a fake daniel-pi the updater suites run `homelab-updater` on."""

import pytest
from _updater_host import Host


@pytest.fixture
def host(tmp_path) -> Host:
    return Host(tmp_path)
