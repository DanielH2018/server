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


def test_state_join_then_leave_accrues_playtime():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("leave", "DBoy", 1060.0)
    p = st.players["DBoy"]
    assert p["total_playtime"] == 60.0
    assert p["sessions"] == 1
    assert p["open_start"] is None
    assert p["first_seen"] == 1000.0
    assert p["last_seen"] == 1060.0


def test_state_first_seen_is_sticky():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("leave", "DBoy", 1060.0)
    st.apply("join", "DBoy", 2000.0)
    st.apply("leave", "DBoy", 2030.0)
    p = st.players["DBoy"]
    assert p["first_seen"] == 1000.0
    assert p["total_playtime"] == 90.0
    assert p["sessions"] == 2


def test_state_leave_without_join_is_noop():
    st = stats.StatsState()
    st.apply("leave", "Ghost", 1000.0)
    assert "Ghost" not in st.players or st.players["Ghost"]["sessions"] == 0


def test_restart_closes_open_sessions():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("join", "Pal", 1010.0)
    st.apply("restart", None, 1100.0)
    assert st.players["DBoy"]["total_playtime"] == 100.0
    assert st.players["Pal"]["total_playtime"] == 90.0
    assert st.online_count() == 0


def test_rejoin_without_leave_closes_prior_session():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("join", "DBoy", 1050.0)   # crash/rejoin, no leave
    assert st.players["DBoy"]["sessions"] == 1
    assert st.players["DBoy"]["total_playtime"] == 50.0
    assert st.players["DBoy"]["open_start"] == 1050.0
    assert st.online_count() == 1


def test_live_playtime_includes_open_session():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    assert st.playtime("DBoy", now=1040.0) == 40.0   # 0 closed + 40 live
    st.apply("leave", "DBoy", 1100.0)
    assert st.playtime("DBoy", now=9999.0) == 100.0  # closed, no live delta
