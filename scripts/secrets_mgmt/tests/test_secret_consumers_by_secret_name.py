"""`consumers` names a role that reads a secret through another role's Secret (#5416).

pihole-exporter never writes `pihole_password`: it reads the value from the `pihole-env`
Secret by name, so a grep for the variable misses it.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_consumers_by_secret_name.py
"""

from secrets_mgmt import secret_rotation as sr


def _consumers_output(capsys, name: str) -> tuple[set[str], list[str]]:
    """The roles `consumers <name>` lists, and the lines under its rotation heading."""
    assert sr.main(["consumers", name]) == 0
    head, _, commands = capsys.readouterr().out.partition(
        "To make a rotation take effect:"
    )
    roles = {line.split()[0] for line in head.splitlines() if line.startswith("  ")}
    return roles, [line.strip() for line in commands.splitlines() if line.strip()]


def test_consumers_of_pihole_password_names_pihole_exporter(capsys):
    roles, _ = _consumers_output(capsys, "pihole_password")
    assert {"pihole", "pihole-exporter"} <= roles, roles


def test_the_rotation_steps_for_pihole_password_cover_pihole_exporter(capsys):
    _, commands = _consumers_output(capsys, "pihole_password")
    assert [c for c in commands if c.startswith("pihole-exporter ")], commands
