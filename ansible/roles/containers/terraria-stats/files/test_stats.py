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


def test_unparsed_player_line_detects_drift():
    # A future console wording that no longer matches the strict patterns.
    assert stats.is_unparsed_player_line("DBoy has joined the game") is True
    assert stats.is_unparsed_player_line("DBoy has left the world") is True


def test_unparsed_player_line_false_for_valid_and_noise():
    assert stats.is_unparsed_player_line("DBoy has joined.") is False
    assert stats.is_unparsed_player_line("Saving world data: 75%") is False
    assert stats.is_unparsed_player_line("1.2.3.4:5 is connecting...") is False
