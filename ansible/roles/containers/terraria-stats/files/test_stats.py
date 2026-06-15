import stats


def test_parse_join():
    assert stats.parse_line("DBoy has joined.") == ("join", "DBoy")


def test_parse_leave():
    assert stats.parse_line("DBoy has left.") == ("leave", "DBoy")


def test_parse_restart_listening():
    assert stats.parse_line("Listening on port 7777") == ("restart", None)


def test_parse_restart_server_started():
    assert stats.parse_line("Server started") == ("restart", None)


def test_parse_name_with_spaces():
    assert stats.parse_line("Big Boss has joined.") == ("join", "Big Boss")


def test_parse_noise_returns_none():
    for line in (
        "172.21.0.15:59682 is connecting...",
        "Saving world data: 75%",
        "Validating world save: 18%",
        "Backing up world file",
        "",
    ):
        assert stats.parse_line(line) is None
