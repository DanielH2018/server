"""Fixtures every probe test shares."""

import gitops_markers
import pytest


@pytest.fixture(autouse=True)
def _agent_tables_hold_k3s(monkeypatch):
    """The printers' tables are empty since H7.34, and H7.53 deletes the printers with the deployer."""
    monkeypatch.setitem(gitops_markers.AGENT_JOIN_TAGS, "k3s", "k3s_agent")
    monkeypatch.setitem(gitops_markers.AGENT_PIN_TAGS, "k3s", "node-address")
